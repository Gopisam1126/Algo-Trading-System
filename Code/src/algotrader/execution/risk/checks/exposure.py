"""Portfolio exposure checks 8–10 (E14-S04).

Where eligibility asked about the instrument, these ask about the **book**: is
this candidate one more of something we already own too much of?

## The design record

**Decision.** Three checks — ``correlation``, ``sector_exposure``,
``net_exposure`` — built by a factory closing over the configured limits, in
that order.

**Sector is the primary control and correlation the secondary one**, which is
the story's own build concern and worth restating because it is not obvious:
four PSU banks can show only *moderate* pairwise correlation and still be one
bet. A correlation guard alone would let them through. The sector cap catches
the linkage that correlation misses, and correlation catches the linkage a
sector label misses — a stock and its listed holding company, or two names
that happen to move together across sector lines.

**Alternative rejected.** Assuming the candidate's worst-case notional
(``max_position_pct``, 20% of capital) and rejecting if
``existing + 20% > cap``. Safe, but badly over-tight: real ATR-based sizes are
typically far below the 20% cap, so this would refuse trades that would
comfortably have fit and the system would sit out for reasons invisible in the
book. Being conservative in the direction of not trading is still wrong when
it is this inaccurate.

**The failure it prevents.** One shock taking four positions at once while
every per-trade limit still reads as satisfied. That is the shape of a
portfolio blow-up: nothing looks wrong per trade.

**What would make this wrong.** If the system ever ran a deliberately
market-neutral book, the net-directional cap would be measuring the wrong
thing — a matched long and short is *less* directional risk, not more, which
is why :meth:`RiskContext.net_exposure` is signed and these checks read the
signed value rather than the gross.

## What these checks CANNOT do, and where it is fixed

**They cannot make the sector and net-directional caps binding.** Risk checks
run *before* sizing — ``RiskEngine.evaluate`` runs every check and only then
calls the sizer — so at check time the candidate has a direction but no
quantity, and an exposure cap is a statement about notional.

So these refuse only when the book is **already** at a cap. A book at 39% of a
40% sector cap passes here, and sizing may then add a further position.
``LOW_LEVEL_ARCHITECTURE.md §5.7``'s sizing formula clamps on position value,
slot capital and broker margin — **there is no sector clamp and no
net-directional clamp** — so today those two limits are enforced by nothing
once the book is under them.

**E14-S10** exists for exactly that: the sizer is the only component that
knows the quantity *and* already applies a ``min()`` over clamps while
recording which one bound. Until it lands, treat these two caps as a backstop
rather than a guarantee.

## Unknown is a rejection, three times over

Every input here has an absent case, and each one defaults to something that
reads as safe and is not:

* a **held position with no correlation entry** — absent, not zero. Zero would
  read as "uncorrelated" and admit the fourth PSU bank.
* an **unknown sector on the candidate** — the old
  ``RiskContext.sector_exposure(None)`` returned ``Decimal(0)``, so an
  unclassified instrument sailed past the primary control. The helper no
  longer accepts ``None``.
* an **unknown sector on an open position** — the same hole from the other
  side: a position with ``sector=None`` matches no sector, so its notional
  escapes every total and the cap never binds.
"""

from __future__ import annotations

from decimal import Decimal

from algotrader.common.enums import RejectReason
from algotrader.common.models.trading import Recommendation
from algotrader.execution.risk.context import RiskContext
from algotrader.execution.risk.framework import CheckOutcome, RiskCheck

#: How many correlated symbols a rejection names before summarising. Same
#: bound and same reason as the other checks: the detail reaches the audit
#: payload and a log line once per rejected candidate per bar. QA-SEC-29/31.
MAX_SYMBOLS_NAMED = 8


def _pct_of(amount: Decimal, capital: Decimal) -> Decimal:
    return (amount / capital) * 100


def _named(symbols: list[str]) -> str:
    shown = ", ".join(sorted(symbols)[:MAX_SYMBOLS_NAMED])
    if len(symbols) > MAX_SYMBOLS_NAMED:
        shown += f", and {len(symbols) - MAX_SYMBOLS_NAMED} more"
    return shown


# ---------------------------------------------------------------------------
# 8. Correlation guard — the SECONDARY control
# ---------------------------------------------------------------------------


def build_correlation_check(max_correlated: int, threshold: Decimal) -> RiskCheck:
    """Reject a candidate that is correlated to too many open positions.

    ``threshold`` is compared against the **absolute** correlation. A pair at
    -0.85 is as much one bet as a pair at +0.85 — taking a long in one and a
    short in the other is a single spread position, not two independent ones,
    and it fails together when the relationship breaks.
    """
    if max_correlated < 1:
        raise ValueError(
            f"max_correlated_positions is {max_correlated}; below 1 the guard "
            f"would reject every candidate the moment any position is open"
        )
    if not (0 < threshold <= 1):
        raise ValueError(
            f"correlation threshold {threshold} is outside (0, 1]. A threshold "
            f"of 0 makes every pair correlated; above 1 makes none."
        )

    def check(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
        if not ctx.open_positions:
            return CheckOutcome.ok()

        unknown: list[str] = []
        corrupt: list[str] = []
        correlated: list[str] = []
        for position in ctx.open_positions:
            rho = ctx.correlations.get(position.symbol)
            if rho is None:
                unknown.append(position.symbol)
            elif not rho.is_finite():
                # NaN and the infinities. `Decimal('NaN') >= threshold` raises
                # InvalidOperation, which the framework would turn into a
                # rejection carrying "InvalidOperation: [<class ...>]" — safe,
                # but telling an operator nothing. A NaN here means the
                # pre-market matrix produced garbage for this pair, and that is
                # what the detail should say.
                corrupt.append(position.symbol)
            elif abs(rho) >= threshold:
                correlated.append(position.symbol)

        if corrupt:
            raise ValueError(
                f"correlation is not a finite number for {len(corrupt)} held "
                f"position(s): {_named(corrupt)}. The pre-market matrix "
                f"produced a NaN or infinity for this pair, so the guard "
                f"cannot be applied."
            )

        if unknown:
            # NOT a business rejection: we could not evaluate the gate. The
            # framework's RISK_ENGINE_FAULT is the honest code, and raising is
            # how a check reaches it (SIT-001).
            raise ValueError(
                f"no correlation available for {len(unknown)} held "
                f"position(s): {_named(unknown)}. Unknown correlation is not "
                f"'uncorrelated' — the guard cannot be applied, so the trade "
                f"is refused."
            )

        if len(correlated) >= max_correlated:
            return CheckOutcome.fail(
                RejectReason.CORRELATION_LIMIT,
                f"{rec.symbol} correlates at or above {threshold} with "
                f"{len(correlated)} open position(s): {_named(correlated)}. "
                f"The limit is {max_correlated}; adding this would make them "
                f"one bet across several slots.",
            )
        return CheckOutcome.ok()

    return RiskCheck(
        id="correlation",
        fn=check,
        description=f"correlated to fewer than {max_correlated} open positions",
    )


# ---------------------------------------------------------------------------
# 9. Sector exposure — the PRIMARY control
# ---------------------------------------------------------------------------


def build_sector_exposure_check(max_pct: Decimal) -> RiskCheck:
    """Reject when this candidate's sector is already at its cap."""
    if not (0 < max_pct <= 100):
        raise ValueError(f"max_sector_exposure_pct {max_pct} is outside (0, 100]")

    def check(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
        unclassified = ctx.positions_missing_a_sector()
        if unclassified:
            raise ValueError(
                f"{len(unclassified)} open position(s) have no sector: "
                f"{_named(list(unclassified))}. Their notional would escape "
                f"every sector total, so the cap cannot be trusted and the "
                f"trade is refused."
            )
        if ctx.symbol_sector is None:
            raise ValueError(
                f"{rec.symbol} has no sector classification, so its exposure "
                f"cannot be attributed. An unclassified instrument must not "
                f"pass the primary concentration control by default."
            )

        held = ctx.sector_exposure(ctx.symbol_sector)
        pct = _pct_of(held, ctx.capital)
        if pct >= max_pct:
            return CheckOutcome.fail(
                RejectReason.SECTOR_EXPOSURE_LIMIT,
                f"sector {ctx.symbol_sector!r} already holds {pct:.1f}% of "
                f"capital against a {max_pct}% cap, so there is no room for "
                f"{rec.symbol}. Concentration, not any single position, is "
                f"what a sector shock acts on.",
            )
        return CheckOutcome.ok()

    return RiskCheck(
        id="sector_exposure",
        fn=check,
        description=f"the candidate's sector is below {max_pct}% of capital",
    )


# ---------------------------------------------------------------------------
# 10. Net directional exposure
# ---------------------------------------------------------------------------


def build_net_exposure_check(max_pct: Decimal) -> RiskCheck:
    """Reject when the book is already too one-sided.

    Reads the **signed** net, so a matched long and short cancel. A book that
    is long 3 lakh and short 3 lakh carries real risk, but not *directional*
    risk, and this check is the one that measures direction. Gross exposure is
    a different limit and not this one.
    """
    if not (0 < max_pct <= 100):
        raise ValueError(f"max_net_directional_exposure_pct {max_pct} is outside (0, 100]")

    def check(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
        net = ctx.net_exposure()
        pct = _pct_of(abs(net), ctx.capital)
        if pct >= max_pct:
            side = "long" if net > 0 else "short"
            return CheckOutcome.fail(
                RejectReason.NET_EXPOSURE_LIMIT,
                f"the book is already {pct:.1f}% net {side} against a "
                f"{max_pct}% cap, so there is no room for {rec.symbol}. A "
                f"one-sided book is a single bet on direction.",
            )
        return CheckOutcome.ok()

    return RiskCheck(
        id="net_exposure",
        fn=check,
        description=f"net directional exposure is below {max_pct}% of capital",
    )


# ---------------------------------------------------------------------------
# The ordered set
# ---------------------------------------------------------------------------


def build_exposure_checks(
    *,
    max_correlated_positions: int,
    correlation_threshold: Decimal,
    max_sector_exposure_pct: Decimal,
    max_net_directional_exposure_pct: Decimal,
) -> tuple[RiskCheck, ...]:
    """The three, in the order they must run.

    Correlation first because it is the narrowest question — it reads only the
    candidate against what is held. Sector next, then net exposure, which is
    the broadest. As everywhere in this pipeline the order decides *which*
    rejection an operator sees, and the more specific reason is the more
    useful one.

    Keyword-only: four numeric limits in a row is exactly the signature where a
    positional call silently swaps two and nothing complains.
    """
    return (
        build_correlation_check(max_correlated_positions, correlation_threshold),
        build_sector_exposure_check(max_sector_exposure_pct),
        build_net_exposure_check(max_net_directional_exposure_pct),
    )


#: The ids, in order, for tests and anything asserting the pipeline shape.
EXPOSURE_ORDER = ("correlation", "sector_exposure", "net_exposure")
