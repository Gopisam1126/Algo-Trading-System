"""Corporate action adjustment.

A 1:5 split on a ₹2,500 stock makes it ₹500 overnight. Unadjusted, the history
shows an 80% crash that never happened and every indicator reading across the
event is wrong.

The obvious fix — rewriting stored prices — works until the *second* action, at
which point adjustments compound with no error and no failing test. So raw
prices are never modified. Each bar carries two factors derived from the action
history:

    adjusted price  = raw price  * price_adj_factor
    adjusted volume = raw volume * volume_adj_factor

Recomputation always rebuilds from the full history, so it is idempotent and a
mis-entered action is fixed by correcting its row and recomputing.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.common.calendar import IST
from algotrader.common.db.models import CorporateAction
from algotrader.common.enums import CorporateActionType

ONE = Decimal(1)

#: Dividends are recorded but not applied to prices. Dividend adjustment is
#: standard for total-return analysis, but it rewrites every historical price
#: slightly, and these are intraday strategies that never hold across an
#: ex-date. Flip this only alongside a decision about what backtests measure.
APPLY_DIVIDEND_ADJUSTMENT = False

_RATIO_ACTIONS = frozenset(
    {
        CorporateActionType.SPLIT,
        CorporateActionType.BONUS,
        CorporateActionType.CONSOLIDATION,
    }
)


class AdjustmentError(ValueError):
    """Raised when an action cannot produce a meaningful factor."""


def bonus_ratio(*, new_shares: Decimal, for_every_held: Decimal) -> tuple[Decimal, Decimal]:
    """``(ratio_from, ratio_to)`` for a bonus announced as "new for every held".

    **The convention this table uses is share COUNTS, old to new** - a 1:5
    split is from=1, to=5: one share becomes five. An Indian bonus is
    announced the other way, as new shares granted per shares held: "a 1:1
    bonus" is one new share for every one held, so ONE share becomes TWO and
    is stored from=1, to=2. Entered as from=1, to=1 it computed a factor of 1
    and adjusted nothing (audit NR-004, 17 Nov 2026). A producer converts here,
    by name, rather than at the call site from a pair of bare numbers.
    """
    if new_shares <= 0 or for_every_held <= 0:
        raise AdjustmentError("a bonus grants a positive number of shares per a positive holding")
    return for_every_held, for_every_held + new_shares


def check_ratio(kind: CorporateActionType, ratio_from: Decimal, ratio_to: Decimal) -> None:
    """Refuse a ratio pointing the wrong way for its action.

    A split or a bonus always ends with MORE shares than it started with, a
    consolidation with fewer. A ratio that says otherwise - a bonus written
    1:1 straight from its announcement, a consolidation entered as a split -
    is a data error with a plausible-looking factor, refused at write and
    again at recompute (NR-004).
    """
    if ratio_from <= 0 or ratio_to <= 0:
        raise AdjustmentError(f"{kind.value} ratios must be positive")
    grows = kind in (CorporateActionType.SPLIT, CorporateActionType.BONUS)
    if grows and ratio_to <= ratio_from:
        raise AdjustmentError(
            f"{kind.value} {ratio_from}:{ratio_to} does not increase the share count. Ratios "
            f"are old:new share COUNTS (a 1:5 split is 1:5; a 1-for-1 bonus is 1:2 - see "
            f"bonus_ratio)"
        )
    if kind is CorporateActionType.CONSOLIDATION and ratio_to >= ratio_from:
        raise AdjustmentError(
            f"CONSOLIDATION {ratio_from}:{ratio_to} does not reduce the share count. Ratios "
            f"are old:new share COUNTS (a 5-into-1 consolidation is 5:1)"
        )


@dataclass(frozen=True, slots=True)
class ActionFactors:
    """One action's effect on bars before its ex-date, held as exact ratios.

    Numerator and denominator are kept separate rather than pre-divided.
    ``Decimal`` division rounds to 28 significant digits, so multiplying
    quotients is only *approximately* associative — combining the same three
    actions in two different orders produced answers differing in the last
    digits. Accumulating the ratios and dividing once makes the combination
    exact, which is what lets recomputation be genuinely order-independent
    rather than nearly so.
    """

    price_num: Decimal = ONE
    price_den: Decimal = ONE
    volume_num: Decimal = ONE
    volume_den: Decimal = ONE

    @property
    def price(self) -> Decimal:
        return self.price_num / self.price_den

    @property
    def volume(self) -> Decimal:
        return self.volume_num / self.volume_den


def factors_for(
    action_type: str | CorporateActionType,
    *,
    ratio_from: Decimal | None = None,
    ratio_to: Decimal | None = None,
    dividend_amount: Decimal | None = None,
    reference_close: Decimal | None = None,
) -> ActionFactors:
    """Factors applied to bars *before* this action's ex-date.

    Price and volume are separate because they are not reciprocal for every
    action. A split restates the share count, so price falls and volume rises.
    A dividend reduces price and leaves volume untouched — one reciprocal factor
    would corrupt volume on every dividend, and volume feeds both the liquidity
    filter and the volume-ratio indicator.
    """
    kind = CorporateActionType(action_type)

    if kind in _RATIO_ACTIONS:
        if ratio_from is None or ratio_to is None:
            raise AdjustmentError(f"{kind.value} needs ratio_from and ratio_to")
        check_ratio(kind, ratio_from, ratio_to)
        # 1:5 split -> one share becomes five -> price x 1/5, volume x 5.
        return ActionFactors(
            price_num=ratio_from,
            price_den=ratio_to,
            volume_num=ratio_to,
            volume_den=ratio_from,
        )

    if kind is CorporateActionType.DIVIDEND:
        if dividend_amount is None:
            raise AdjustmentError("DIVIDEND needs dividend_amount")
        if not APPLY_DIVIDEND_ADJUSTMENT:
            return ActionFactors()
        if reference_close is None or reference_close <= 0:
            raise AdjustmentError("dividend adjustment needs the close before the ex-date")
        if dividend_amount >= reference_close:
            raise AdjustmentError(
                f"dividend {dividend_amount} is not less than the reference close "
                f"{reference_close}; that is bad source data, not a 100% price cut"
            )
        return ActionFactors(price_num=reference_close - dividend_amount, price_den=reference_close)

    # RIGHTS needs the subscription price and ratio modelled properly. A visible
    # no-op is better than a wrong guess.
    return ActionFactors()


def cumulative_factors(
    actions: Sequence[tuple[dt.date, ActionFactors]], bar_date: dt.date
) -> ActionFactors:
    """Combine every action taking effect strictly after ``bar_date``.

    Ratios accumulate and divide once, so the result is independent of the order
    actions are supplied in — actions arrive from a feed in whatever order the
    source lists them, and two runs over the same data must agree exactly.
    """
    price_num = price_den = volume_num = volume_den = ONE
    for ex_date, factors in actions:
        if ex_date > bar_date:
            price_num *= factors.price_num
            price_den *= factors.price_den
            volume_num *= factors.volume_num
            volume_den *= factors.volume_den
    return ActionFactors(
        price_num=price_num,
        price_den=price_den,
        volume_num=volume_num,
        volume_den=volume_den,
    )


class CorporateActionRepository:
    """Reads and writes actions, and recomputes the factors derived from them."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, actions: Sequence[dict[str, Any]]) -> int:
        """Record actions. Idempotent on (symbol_id, action_type, ex_date).

        Re-fetching the same announcement must update rather than insert: a
        double-counted split halves every historical price a second time.
        """
        if not actions:
            return 0
        for action in actions:
            kind = CorporateActionType(action["action_type"])
            low, high = action.get("ratio_from"), action.get("ratio_to")
            if kind in _RATIO_ACTIONS and low is not None and high is not None:
                check_ratio(kind, Decimal(low), Decimal(high))
        stmt = pg_insert(CorporateAction).values(list(actions))
        stmt = stmt.on_conflict_do_update(
            constraint="uq_action",
            set_={
                "ratio_from": stmt.excluded.ratio_from,
                "ratio_to": stmt.excluded.ratio_to,
                "dividend_amount": stmt.excluded.dividend_amount,
                "announced_at": stmt.excluded.announced_at,
                "source": stmt.excluded.source,
                "fetched_at": stmt.excluded.fetched_at,
            },
        )
        await self._session.execute(stmt)
        return len(actions)

    async def for_symbol(self, symbol_id: int) -> list[dict[str, Any]]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT action_type, ex_date, ratio_from, ratio_to, dividend_amount "
                    "FROM corporate_action WHERE symbol_id = :sid ORDER BY ex_date"
                ),
                {"sid": symbol_id},
            )
        ).all()
        return [
            {
                "action_type": r[0],
                "ex_date": r[1],
                "ratio_from": r[2],
                "ratio_to": r[3],
                "dividend_amount": r[4],
            }
            for r in rows
        ]

    async def recompute_factors(self, symbol_id: int) -> int:
        """Rebuild both factor columns for one symbol from its full action history.

        Always from scratch rather than patched incrementally — that is what
        makes it idempotent. Returns the number of bars updated.

        Each ex-date opens a segment and every bar in a segment shares one factor
        pair, so a symbol with three actions needs four ranged UPDATEs rather
        than one statement per bar.

        The advisory lock closes a lost update. Two recomputes for the same
        symbol can otherwise interleave: A reads the action list, B inserts a
        newly-announced bonus and recomputes with both actions, then A commits
        the factors it derived from the shorter list and the bonus is silently
        undone. Row locks do not prevent this — both transactions write the same
        rows with self-consistent values, so the database sees nothing wrong.
        Taking the lock before the read makes each recompute observe every action
        committed before it. Transaction-scoped, so it releases on commit or
        rollback with nothing to clean up — the same reasoning as the audit
        chain's lock.
        """
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('ca_recompute'), :sid)"),
            {"sid": symbol_id},
        )
        actions = await self.for_symbol(symbol_id)
        if not actions:
            result = await self._session.execute(
                text(
                    "UPDATE ohlcv SET price_adj_factor = 1.0, volume_adj_factor = 1.0 "
                    "WHERE symbol_id = :sid "
                    "AND (price_adj_factor <> 1.0 OR volume_adj_factor <> 1.0)"
                ),
                {"sid": symbol_id},
            )
            return int(cast("CursorResult[Any]", result).rowcount or 0)

        resolved = [
            (
                a["ex_date"],
                factors_for(
                    a["action_type"],
                    ratio_from=a["ratio_from"],
                    ratio_to=a["ratio_to"],
                    dividend_amount=a["dividend_amount"],
                ),
            )
            for a in actions
        ]

        boundaries = sorted({ex for ex, _ in resolved})
        updated = 0
        lower: dt.date | None = None
        for boundary in [*boundaries, None]:
            reference = boundary - dt.timedelta(days=1) if boundary else dt.date.max
            combined = cumulative_factors(resolved, reference)
            # Each boundary is the ex-date's MIDNIGHT IN IST, bound as an
            # instant. `ts < CAST(:hi AS date)` compared a timestamptz with a
            # bare date, which PostgreSQL turns into midnight in the SESSION's
            # TimeZone: on a server west of UTC-03:45 the ex-date's first bars
            # kept the pre-ex factor (audit ENG-029, 17 Nov 2026).
            sql = (
                "UPDATE ohlcv SET price_adj_factor = :p, volume_adj_factor = :v "
                "WHERE symbol_id = :sid"
            )
            params: dict[str, Any] = {"sid": symbol_id, "p": combined.price, "v": combined.volume}
            if lower is not None:
                sql += " AND ts >= :lo"
                params["lo"] = ist_midnight(lower)
            if boundary is not None:
                sql += " AND ts < :hi"
                params["hi"] = ist_midnight(boundary)
            result = await self._session.execute(text(sql), params)
            updated += int(cast("CursorResult[Any]", result).rowcount or 0)
            lower = boundary
        return updated

    async def symbols_needing_recompute(self, earliest: dict[int, dt.date]) -> list[int]:
        """The symbols, of ``{symbol_id: earliest bar date}``, with an action AFTER it.

        A bar inserted before a recorded ex-date gets the column default factor
        of 1 - correct only if no action follows it. These are the symbols for
        which that default is wrong.
        """
        if not earliest:
            return []
        rows = (
            await self._session.execute(
                text(
                    "SELECT symbol_id, max(ex_date) FROM corporate_action "
                    "WHERE symbol_id = ANY(:sids) GROUP BY symbol_id"
                ),
                {"sids": list(earliest)},
            )
        ).all()
        return sorted(int(sid) for sid, newest in rows if newest > earliest[int(sid)])

    async def recompute_all(self, symbol_ids: Sequence[int]) -> int:
        total = 0
        for symbol_id in symbol_ids:
            total += await self.recompute_factors(symbol_id)
        return total


def ist_midnight(day: dt.date) -> dt.datetime:
    """The instant an IST trading day begins, in UTC - what an ex-date means."""
    return dt.datetime.combine(day, dt.time(0), tzinfo=IST).astimezone(dt.UTC)
