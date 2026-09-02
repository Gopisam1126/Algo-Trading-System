"""The rolling correlation matrix the portfolio guard reads (E14-S04).

A pure function over price series. It does no I/O and touches no clock, so it
can be computed wherever the history already is — the pre-market pipeline —
and handed to the risk engine as a value, like the calendar.

## The design record

**Decision.** Pearson correlation of **daily log returns over 60 trading
sessions**, recomputed **pre-market and never intraday**.

**The alternative rejected**, and why each way of being wrong is wrong:

*Intraday sampling.* Tempting, because an intraday system cares about today.
But a correlation estimated from high-frequency samples of two separately
traded names is biased **toward zero**, because their prints are not
synchronous — two stocks rarely trade in the same instant, so each sampling
interval catches one moving and the other stale. The estimate would
systematically *understate* how alike two PSU banks are, which is precisely
the failure this guard exists to prevent. Wrong in the dangerous direction.

*A short window (20 sessions).* Flaps. The guard would admit a correlated pair
one morning and refuse it the next on unchanged fundamentals, and neither
answer would be explainable to an operator.

*A long window (250 sessions).* Misses regime change. Two names that decoupled
three months ago still read as one bet.

**The failure it prevents.** Four names that are one bet occupying four slots,
so a single sector shock takes four positions at once while every per-trade
limit still reads as satisfied.

**What would make this wrong.** 60 sessions is a **judgement, not a derived
fact** — recorded as an assumption. If the guard proves noisy, shorten it; if
it proves slow to react, the sector cap is the primary control and should
carry more of the load. Also: correlation is *not* the only linkage. Two names
can be one bet at moderate correlation, which is exactly why
``LOW_LEVEL_ARCHITECTURE.md`` and this story both make the **sector cap
primary and correlation secondary**.

## Why log returns

Log returns are additive across time and symmetric between a rise and the
matching fall, so a 10% gain followed by a 10% loss is not counted as a net
move. Simple returns are neither, and the asymmetry biases correlation between
a volatile name and a quiet one.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from itertools import pairwise

#: Trading sessions of daily returns behind each correlation estimate.
#:
#: A judgement, not a derived constant. ~one quarter: long enough to be stable,
#: short enough to reflect the current regime. See the module docstring.
CORRELATION_WINDOW_SESSIONS = 60

#: The fewest returns that may stand behind a correlation at all.
#:
#: Below this the estimate is noise wearing a number, and a *number* is far
#: more dangerous than a gap — the guard would act on it. 30 daily returns is
#: already thin; it is a floor, not a target.
MIN_SESSIONS_FOR_CORRELATION = 30


class CorrelationError(ValueError):
    """The inputs cannot support a correlation, so none is produced."""


def log_returns(closes: Sequence[Decimal]) -> list[float]:
    """Daily log returns from a close series, oldest first.

    ``float`` rather than ``Decimal`` deliberately, and this is the one place
    in the risk path where that is right: correlation is a statistical
    estimate, not money. It is never summed into a rupee amount, never
    compared against capital and never sent to a broker — it only ever meets a
    threshold. Carrying it as Decimal would imply an exactness the estimate
    does not have. Every value that *is* money stays Decimal.
    """
    if len(closes) < 2:
        raise CorrelationError(f"need at least 2 closes to form a return, got {len(closes)}")
    out: list[float] = []
    for previous, current in pairwise(closes):
        if previous <= 0 or current <= 0:
            raise CorrelationError(
                f"non-positive close in the series ({previous} -> {current}); a "
                f"log return is undefined. A zero close is bad data, not a "
                f"100% loss."
            )
        out.append(math.log(float(current) / float(previous)))
    return out


def pearson(left: Sequence[float], right: Sequence[float]) -> float:
    """Pearson correlation of two equal-length return series.

    Raises rather than returning 0.0 when either series has no variance. A flat
    series has an undefined correlation with anything, and 0.0 would read as
    "independent" — a confident claim in place of a missing one, which the
    caller would act on.
    """
    if len(left) != len(right):
        raise CorrelationError(
            f"series lengths differ ({len(left)} vs {len(right)}); a correlation "
            f"across mismatched dates is meaningless"
        )
    n = len(left)
    if n < MIN_SESSIONS_FOR_CORRELATION:
        raise CorrelationError(
            f"{n} returns is below the {MIN_SESSIONS_FOR_CORRELATION}-session "
            f"floor; the estimate would be noise wearing a number"
        )
    mean_l = sum(left) / n
    mean_r = sum(right) / n
    cov = sum((a - mean_l) * (b - mean_r) for a, b in zip(left, right, strict=True))
    var_l = sum((a - mean_l) ** 2 for a in left)
    var_r = sum((b - mean_r) ** 2 for b in right)
    if var_l <= 0 or var_r <= 0:
        raise CorrelationError(
            "a series has zero variance, so its correlation is undefined. "
            "Returning 0.0 here would assert independence rather than admit "
            "the absence of an answer."
        )
    rho = cov / math.sqrt(var_l * var_r)
    # Clamp only floating-point overshoot at the boundaries, never a real value.
    return max(-1.0, min(1.0, rho))


def correlations_against(
    candidate: str,
    closes_by_symbol: Mapping[str, Sequence[Decimal]],
    *,
    against: Sequence[str],
    window: int = CORRELATION_WINDOW_SESSIONS,
) -> dict[str, Decimal]:
    """Correlation of ``candidate`` against each of ``against``.

    This is the shape :attr:`RiskContext.correlations` wants: the candidate
    against each open position, by symbol. A full N x N matrix over the whole
    watchlist is the same computation repeated, and is what the pre-market
    pipeline will cache — but the risk engine only ever needs one row, so one
    row is what this returns.

    A symbol whose correlation **cannot** be computed is **absent from the
    result**, never present as zero. The check reads that absence as "unknown"
    and refuses; a zero would read as "uncorrelated" and let it through.
    """
    if candidate not in closes_by_symbol:
        raise CorrelationError(f"no close series for the candidate {candidate!r}")

    candidate_returns = log_returns(list(closes_by_symbol[candidate])[-(window + 1) :])

    out: dict[str, Decimal] = {}
    for symbol in against:
        series = closes_by_symbol.get(symbol)
        if series is None:
            continue
        try:
            other = log_returns(list(series)[-(window + 1) :])
            paired = min(len(candidate_returns), len(other))
            rho = pearson(candidate_returns[-paired:], other[-paired:])
        except CorrelationError:
            # Deliberately swallowed HERE and nowhere else: the contract of
            # this function is "what could be computed". The caller's contract
            # is that a missing key means unknown, and the check that reads it
            # rejects on unknown. Recording a partial matrix is useful; a
            # matrix with fabricated zeroes is not.
            continue
        out[symbol] = Decimal(str(round(rho, 6)))
    return out
