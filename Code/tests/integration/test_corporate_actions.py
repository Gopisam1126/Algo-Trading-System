"""Corporate action adjustment — the E03-S02 blocker resolution.

Safety-critical. A wrong adjustment does not raise; it silently rewrites price
history, and every backtest over the affected symbol becomes fiction.

The three properties that make the design trustworthy, each tested here:

- **Order independence** — factors do not depend on the order actions arrive in
- **Idempotency** — recomputing twice changes nothing
- **Continuity** — a known split produces an unbroken series across the event
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.common.db import corporate_actions as ca_module
from algotrader.common.db import engine as db_engine
from algotrader.common.db.corporate_actions import (
    ActionFactors,
    AdjustmentError,
    CorporateActionRepository,
    cumulative_factors,
    factors_for,
)
from algotrader.common.db.repositories import BarRepository, InstrumentRepository
from algotrader.common.enums import CorporateActionType, Timeframe

pytestmark = [pytest.mark.integration]

SPLIT_DATE = dt.date(2026, 6, 15)


@pytest.fixture
async def session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    eng = db_engine.create_engine_from_url(migrated_database)
    factory = db_engine.create_session_factory(eng)
    async with factory() as s:
        await s.execute(text("DELETE FROM corporate_action"))
        await s.execute(text("DELETE FROM ohlcv"))
        await s.commit()
        yield s
        await s.rollback()
    await eng.dispose()


@pytest.fixture
async def symbol_id(session: AsyncSession) -> int:
    repo = InstrumentRepository(session)
    await repo.upsert(
        [
            {
                "tradingsymbol": "SPLITCO",
                "exchange": "NSE",
                "broker_token": "sp1",
                "tick_size": Decimal("0.05"),
            }
        ]
    )
    await session.flush()
    return await repo.symbol_id("SPLITCO")


async def _seed_daily_bars(session: AsyncSession, symbol_id: int, days: int = 20) -> None:
    """Bars either side of SPLIT_DATE, priced 2500 before and 500 after.

    That is what a real 1:5 split looks like on the tape: the raw series has an
    80% cliff which is not a price move at all.
    """
    repo = InstrumentRepository(session)
    await repo.refresh_cache()
    bars = BarRepository(session, repo)
    rows = []
    for offset in range(-days, days):
        day = SPLIT_DATE + dt.timedelta(days=offset)
        pre = day < SPLIT_DATE
        base = Decimal("2500") if pre else Decimal("500")
        rows.append(
            {
                "symbol_id": symbol_id,
                "timeframe": "1d",
                "ts": dt.datetime.combine(day, dt.time(3, 45), tzinfo=dt.UTC),
                "open": base,
                "high": base * Decimal("1.01"),
                "low": base * Decimal("0.99"),
                "close": base,
                "volume": 1_000 if pre else 5_000,
            }
        )
    await bars.bulk_upsert(rows)
    await session.flush()


class TestFactorArithmetic:
    """Pure functions — no database needed."""

    def test_split_scales_price_down_and_volume_up(self) -> None:
        f = factors_for(CorporateActionType.SPLIT, ratio_from=Decimal(1), ratio_to=Decimal(5))
        assert f.price == Decimal("0.2")
        assert f.volume == Decimal(5)

    def test_bonus_behaves_like_a_split(self) -> None:
        f = factors_for(CorporateActionType.BONUS, ratio_from=Decimal(1), ratio_to=Decimal(2))
        assert f.price == Decimal("0.5")
        assert f.volume == Decimal(2)

    def test_dividend_leaves_volume_alone(self) -> None:
        """A single reciprocal factor would corrupt volume on every dividend."""
        f = factors_for(CorporateActionType.DIVIDEND, dividend_amount=Decimal(10))
        assert f.volume == Decimal(1)

    def test_ratio_action_without_a_ratio_is_refused(self) -> None:
        """Silently returning 1.0 would make the action a no-op with no error."""
        with pytest.raises(AdjustmentError):
            factors_for(CorporateActionType.SPLIT)

    def test_dividend_without_an_amount_is_refused(self) -> None:
        with pytest.raises(AdjustmentError):
            factors_for(CorporateActionType.DIVIDEND)

    def test_only_actions_after_the_bar_apply(self) -> None:
        split = (
            SPLIT_DATE,
            ActionFactors(
                price_num=Decimal(1),
                price_den=Decimal(5),
                volume_num=Decimal(5),
                volume_den=Decimal(1),
            ),
        )
        before = cumulative_factors([split], SPLIT_DATE - dt.timedelta(days=1))
        on_or_after = cumulative_factors([split], SPLIT_DATE)
        assert before.price == Decimal("0.2")
        assert on_or_after.price == Decimal(1)


class TestOrderIndependence:
    """The property that makes recomputation safe."""

    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        st.lists(
            st.tuples(
                st.integers(min_value=1, max_value=400),
                st.integers(min_value=1, max_value=10),
                st.integers(min_value=2, max_value=10),
            ),
            min_size=1,
            max_size=6,
            unique_by=lambda t: t[0],
        )
    )
    def test_factors_do_not_depend_on_the_order_actions_arrive(
        self, raw: list[tuple[int, int, int]]
    ) -> None:
        """A late-announced action must not change the answer.

        Actions arrive from a feed in whatever order the source lists them. If
        the result depended on that order, two runs over the same data would
        disagree and neither would be identifiably wrong.
        """
        actions = [
            (
                dt.date(2026, 1, 1) + dt.timedelta(days=offset),
                factors_for(
                    CorporateActionType.SPLIT,
                    ratio_from=Decimal(a),
                    ratio_to=Decimal(b),
                ),
            )
            for offset, a, b in raw
        ]
        bar_day = dt.date(2025, 12, 31)

        forward = cumulative_factors(actions, bar_day)
        backward = cumulative_factors(list(reversed(actions)), bar_day)
        shuffled = cumulative_factors(sorted(actions, key=lambda x: str(x[1].price)), bar_day)

        assert forward.price == backward.price == shuffled.price
        assert forward.volume == backward.volume == shuffled.volume


class TestAgainstTheDatabase:
    async def test_a_known_split_produces_a_continuous_series(
        self, session: AsyncSession, symbol_id: int
    ) -> None:
        """E03-S02's acceptance criterion.

        Raw prices show an 80% cliff. Adjusted, the series is flat across the
        event because nothing actually happened to the value of the holding.
        """
        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        await actions.upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "action_type": "SPLIT",
                    "ex_date": SPLIT_DATE,
                    "ratio_from": Decimal(1),
                    "ratio_to": Decimal(5),
                    "source": "test",
                }
            ]
        )
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await session.flush()

        repo = InstrumentRepository(session)
        await repo.refresh_cache()
        bars = BarRepository(session, repo)
        series = await bars.range(
            "SPLITCO",
            Timeframe.D1,
            dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        )
        closes = [row["close"] for row in series]

        assert all(c == Decimal(500) for c in closes), (
            f"adjusted series is not continuous across the split: {sorted(set(closes))}"
        )

    async def test_volume_is_scaled_the_other_way(
        self, session: AsyncSession, symbol_id: int
    ) -> None:
        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        await actions.upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "action_type": "SPLIT",
                    "ex_date": SPLIT_DATE,
                    "ratio_from": Decimal(1),
                    "ratio_to": Decimal(5),
                    "source": "test",
                }
            ]
        )
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await session.flush()

        repo = InstrumentRepository(session)
        await repo.refresh_cache()
        bars = BarRepository(session, repo)
        series = await bars.range(
            "SPLITCO",
            Timeframe.D1,
            dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        )
        assert {row["volume"] for row in series} == {5_000}

    async def test_recompute_is_idempotent(self, session: AsyncSession, symbol_id: int) -> None:
        """Running twice must not compound. This is the whole design."""
        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        await actions.upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "action_type": "SPLIT",
                    "ex_date": SPLIT_DATE,
                    "ratio_from": Decimal(1),
                    "ratio_to": Decimal(5),
                    "source": "test",
                }
            ]
        )
        await session.flush()

        await actions.recompute_factors(symbol_id)
        await session.flush()
        first = await self._factors(session, symbol_id)

        for _ in range(3):
            await actions.recompute_factors(symbol_id)
            await session.flush()
        assert await self._factors(session, symbol_id) == first

    async def test_a_second_action_does_not_compound_the_first(
        self, session: AsyncSession, symbol_id: int
    ) -> None:
        """The failure the boolean `is_adjusted` could not prevent.

        Two 1:2 splits before a bar mean that bar's price divides by 4 — once,
        not once per recompute. Adjusting in place would have applied the second
        on top of an already-adjusted price and produced 1/8 or worse, silently.
        """
        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        await actions.upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "action_type": "SPLIT",
                    "ex_date": SPLIT_DATE,
                    "ratio_from": Decimal(1),
                    "ratio_to": Decimal(2),
                    "source": "test",
                },
                {
                    "symbol_id": symbol_id,
                    "action_type": "BONUS",
                    "ex_date": SPLIT_DATE + dt.timedelta(days=3),
                    "ratio_from": Decimal(1),
                    "ratio_to": Decimal(2),
                    "source": "test",
                },
            ]
        )
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await actions.recompute_factors(symbol_id)
        await session.flush()

        earliest = (
            await session.execute(
                text("SELECT price_adj_factor FROM ohlcv WHERE symbol_id = :s ORDER BY ts LIMIT 1"),
                {"s": symbol_id},
            )
        ).scalar_one()
        assert Decimal(earliest) == Decimal("0.25"), (
            f"expected 1/2 * 1/2 = 0.25, got {earliest} — the adjustment compounded"
        )

    async def test_correcting_a_bad_action_fully_recovers(
        self, session: AsyncSession, symbol_id: int
    ) -> None:
        """Raw prices are never touched, so a wrong action is reversible.

        Under in-place adjustment the original price is gone and this is
        unrecoverable.
        """
        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        wrong = {
            "symbol_id": symbol_id,
            "action_type": "SPLIT",
            "ex_date": SPLIT_DATE,
            "ratio_from": Decimal(1),
            "ratio_to": Decimal(50),
            "source": "test",
        }
        await actions.upsert([wrong])
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await session.flush()

        await actions.upsert([{**wrong, "ratio_to": Decimal(5)}])
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await session.flush()

        repo = InstrumentRepository(session)
        await repo.refresh_cache()
        bars = BarRepository(session, repo)
        series = await bars.range(
            "SPLITCO",
            Timeframe.D1,
            dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        )
        assert {row["close"] for row in series} == {Decimal(500)}

    async def test_removing_all_actions_restores_raw_prices(
        self, session: AsyncSession, symbol_id: int
    ) -> None:
        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        await actions.upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "action_type": "SPLIT",
                    "ex_date": SPLIT_DATE,
                    "ratio_from": Decimal(1),
                    "ratio_to": Decimal(5),
                    "source": "test",
                }
            ]
        )
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await session.execute(
            text("DELETE FROM corporate_action WHERE symbol_id = :s"), {"s": symbol_id}
        )
        await actions.recompute_factors(symbol_id)
        await session.flush()

        factors = await self._factors(session, symbol_id)
        assert set(factors) == {(Decimal(1), Decimal(1))}

    async def test_duplicate_action_ingestion_does_not_double_count(
        self, session: AsyncSession, symbol_id: int
    ) -> None:
        """Re-fetching the same announcement is normal; halving prices twice is not."""
        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        row = {
            "symbol_id": symbol_id,
            "action_type": "SPLIT",
            "ex_date": SPLIT_DATE,
            "ratio_from": Decimal(1),
            "ratio_to": Decimal(5),
            "source": "test",
        }
        await actions.upsert([row])
        await actions.upsert([row])
        await actions.upsert([row])
        await session.flush()

        count = (
            await session.execute(
                text("SELECT count(*) FROM corporate_action WHERE symbol_id = :s"),
                {"s": symbol_id},
            )
        ).scalar_one()
        assert count == 1

    @staticmethod
    async def _factors(session: AsyncSession, symbol_id: int) -> set[tuple[Decimal, Decimal]]:
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT price_adj_factor, volume_adj_factor "
                    "FROM ohlcv WHERE symbol_id = :s"
                ),
                {"s": symbol_id},
            )
        ).all()
        return {(Decimal(r[0]), Decimal(r[1])) for r in rows}


class TestBr16RawPricesAreNotReachable:
    """The trading path must not be able to ask for unadjusted data."""

    def test_bar_repository_exposes_no_raw_read_in_the_normal_api(self) -> None:
        public = {
            name
            for name in dir(BarRepository)
            if not name.startswith("_") and callable(getattr(BarRepository, name))
        }
        assert "raw_bars_for_audit" in public
        assert not (public - {"raw_bars_for_audit"}) & {"latest_n_raw", "range_raw", "raw_bars"}

    async def test_the_audit_accessor_returns_genuinely_raw_values(
        self, session: AsyncSession, symbol_id: int
    ) -> None:
        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        await actions.upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "action_type": "SPLIT",
                    "ex_date": SPLIT_DATE,
                    "ratio_from": Decimal(1),
                    "ratio_to": Decimal(5),
                    "source": "test",
                }
            ]
        )
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await session.flush()

        repo = InstrumentRepository(session)
        await repo.refresh_cache()
        bars = BarRepository(session, repo)

        raw = await bars.raw_bars_for_audit("SPLITCO", Timeframe.D1, 40)
        pre_split = [r for r in raw if r["open_ts"].date() < SPLIT_DATE]
        assert pre_split and all(r["close"] == Decimal(2500) for r in pre_split), (
            "the audit accessor returned adjusted prices; it must return the tape"
        )


class TestAdjustedBarsSurviveTheDomainModel:
    """The repository's output must be constructible as a ``Bar``.

    Every other test here asserts on database values or on factor arithmetic,
    and all of them passed while a 1:3 split made the pre-market warm-up
    unusable: ``Price`` is ``Field(decimal_places=4)``, the factor is stored as
    NUMERIC(18,10), and raw * factor is 14 decimal places. The failure only
    appears for ratios that are not terminating decimals, and only once
    something actually builds a domain object from the read.
    """

    @pytest.mark.parametrize(
        ("ratio_from", "ratio_to"),
        [(1, 3), (1, 6), (2, 3), (1, 7), (3, 7), (1, 5), (1, 2)],
    )
    async def test_a_repeating_ratio_still_produces_a_valid_bar(
        self, session: AsyncSession, symbol_id: int, ratio_from: int, ratio_to: int
    ) -> None:
        from algotrader.common.models.market import Bar

        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        await actions.upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "action_type": "SPLIT",
                    "ex_date": SPLIT_DATE,
                    "ratio_from": Decimal(ratio_from),
                    "ratio_to": Decimal(ratio_to),
                    "source": "test",
                }
            ]
        )
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await session.flush()

        repo = InstrumentRepository(session)
        await repo.refresh_cache()
        bars = BarRepository(session, repo)

        rows = await bars.latest_n("SPLITCO", Timeframe.D1, 40)
        assert rows, "no bars came back"
        for row in rows:
            # Raises decimal_max_places if the repository hands back an
            # unquantised product.
            Bar(
                symbol=row["symbol"],
                timeframe=row["timeframe"],
                open_ts=row["open_ts"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                vwap=row["vwap"],
            )

    async def test_warm_up_batch_output_is_also_model_ready(
        self, session: AsyncSession, symbol_id: int
    ) -> None:
        """The pre-market path specifically — it is the one with a deadline."""
        from algotrader.common.models.market import Bar

        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        await actions.upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "action_type": "SPLIT",
                    "ex_date": SPLIT_DATE,
                    "ratio_from": Decimal(1),
                    "ratio_to": Decimal(3),
                    "source": "test",
                }
            ]
        )
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await session.flush()

        repo = InstrumentRepository(session)
        await repo.refresh_cache()
        bars = BarRepository(session, repo)

        batch = await bars.warm_up_batch(["SPLITCO"], Timeframe.D1, 30)
        assert batch["SPLITCO"], "warm_up_batch returned nothing"
        for row in batch["SPLITCO"]:
            Bar(
                symbol=row["symbol"],
                timeframe=row["timeframe"],
                open_ts=row["open_ts"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                vwap=row["vwap"],
            )

    async def test_rounding_cannot_break_ohlc_coherence(
        self, session: AsyncSession, symbol_id: int
    ) -> None:
        """Quantising each field separately must not invert high/low.

        Rounding is monotonic so this holds, but it is the assumption the
        per-field quantisation rests on and it is cheap to pin down.
        """
        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        await actions.upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "action_type": "SPLIT",
                    "ex_date": SPLIT_DATE,
                    "ratio_from": Decimal(1),
                    "ratio_to": Decimal(7),
                    "source": "test",
                }
            ]
        )
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await session.flush()

        repo = InstrumentRepository(session)
        await repo.refresh_cache()
        rows = await BarRepository(session, repo).latest_n("SPLITCO", Timeframe.D1, 40)
        for row in rows:
            assert row["high"] >= row["low"]
            assert row["high"] >= row["open"] and row["high"] >= row["close"]
            assert row["low"] <= row["open"] and row["low"] <= row["close"]


class TestConcurrentRecomputeIsSerialised:
    async def test_recompute_holds_an_advisory_lock_for_the_symbol(
        self, session: AsyncSession, symbol_id: int, migrated_database: str
    ) -> None:
        """Without this lock two recomputes can interleave and lose an action.

        Checked by observing ``pg_locks`` from a second connection rather than by
        racing two tasks and timing them — the lock is either held at this point
        in the transaction or it is not, and that is a fact the catalog can state
        directly.
        """
        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        await actions.recompute_factors(symbol_id)

        other = db_engine.create_engine_from_url(migrated_database)
        try:
            async with db_engine.create_session_factory(other)() as observer:
                held = (
                    await observer.execute(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                            "AND objid = :sid AND granted"
                        ),
                        {"sid": symbol_id},
                    )
                ).scalar_one()
        finally:
            await other.dispose()

        assert held == 1, "recompute_factors did not hold the per-symbol advisory lock"


class TestIngestDoesNotTouchFactors:
    """Only ``recompute_factors`` may write the factor columns.

    The ingest path leaves them out of both its column list and its ON CONFLICT
    assignments, so the columns keep whatever the last recompute set. That is
    correct by omission, which is exactly the kind of correctness a later edit
    removes without noticing — adding the factors to ``_BAR_COLUMNS`` for
    symmetry would silently reset every adjusted bar to raw on the next
    backfill. This test fails if that happens.
    """

    async def test_re_ingesting_a_bar_preserves_its_adjustment(
        self, session: AsyncSession, symbol_id: int
    ) -> None:
        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        await actions.upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "action_type": "SPLIT",
                    "ex_date": SPLIT_DATE,
                    "ratio_from": Decimal(1),
                    "ratio_to": Decimal(5),
                    "source": "test",
                }
            ]
        )
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await session.flush()

        # Re-run the exact backfill that already loaded these bars.
        await _seed_daily_bars(session, symbol_id)
        await session.flush()

        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT price_adj_factor FROM ohlcv "
                    "WHERE symbol_id = :s AND ts < CAST(:d AS date)"
                ),
                {"s": symbol_id, "d": SPLIT_DATE},
            )
        ).all()
        assert {Decimal(r[0]) for r in rows} == {Decimal("0.2")}, (
            "re-ingesting bars reset their adjustment factors to raw"
        )


class TestActionTypesNotYetExercised:
    """Branches coverage showed were never executed.

    Two of them matter more than the numbers suggest. The dividend-adjustment
    body is dormant behind ``APPLY_DIVIDEND_ADJUSTMENT = False``, so the day
    someone flips that flag they run code no test has ever touched — and it
    rewrites every historical price. The consolidation path had never been run
    against the database at all, only in factor arithmetic.
    """

    def test_a_zero_or_negative_ratio_is_refused(self) -> None:
        """A zero denominator is a crash; a negative one silently flips prices."""
        for a, b in ((0, 5), (5, 0), (-1, 5), (5, -1)):
            with pytest.raises(AdjustmentError, match="positive"):
                factors_for(
                    CorporateActionType.SPLIT,
                    ratio_from=Decimal(a),
                    ratio_to=Decimal(b),
                )

    def test_rights_is_an_explicit_no_op_not_a_silent_one(self) -> None:
        """RIGHTS needs the subscription price modelled; 1.0 is the honest answer."""
        f = factors_for(CorporateActionType.RIGHTS)
        assert f.price == Decimal(1)
        assert f.volume == Decimal(1)

    async def test_a_consolidation_raises_price_and_lowers_volume(
        self, session: AsyncSession, symbol_id: int
    ) -> None:
        """A 5:1 reverse split - the mirror image of a split, never DB-tested."""
        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        await actions.upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "action_type": "CONSOLIDATION",
                    "ex_date": SPLIT_DATE,
                    "ratio_from": Decimal(5),
                    "ratio_to": Decimal(1),
                    "source": "test",
                }
            ]
        )
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await session.flush()

        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT price_adj_factor, volume_adj_factor FROM ohlcv "
                    "WHERE symbol_id = :s AND ts < CAST(:d AS date)"
                ),
                {"s": symbol_id, "d": SPLIT_DATE},
            )
        ).all()
        assert {(Decimal(r[0]), Decimal(r[1])) for r in rows} == {(Decimal(5), Decimal("0.2"))}, (
            "a consolidation must raise historical price and cut historical volume"
        )

    async def test_a_dividend_leaves_prices_untouched_while_disabled(
        self, session: AsyncSession, symbol_id: int
    ) -> None:
        """The shipped default. Recorded for audit, never applied."""
        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        await actions.upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "action_type": "DIVIDEND",
                    "ex_date": SPLIT_DATE,
                    "dividend_amount": Decimal(25),
                    "source": "test",
                }
            ]
        )
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await session.flush()

        factors = await TestAgainstTheDatabase._factors(session, symbol_id)
        assert factors == {(Decimal(1), Decimal(1))}

    def test_the_dormant_dividend_formula_is_correct_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exercised behind the flag so flipping it is not a leap of faith.

        A 25 rupee dividend on a 500 close leaves 95% of the price, and volume
        must not move - that asymmetry is the whole reason two factors exist.
        """
        monkeypatch.setattr(ca_module, "APPLY_DIVIDEND_ADJUSTMENT", True)
        f = factors_for(
            CorporateActionType.DIVIDEND,
            dividend_amount=Decimal(25),
            reference_close=Decimal(500),
        )
        assert f.price == Decimal("0.95")
        assert f.volume == Decimal(1), "a dividend must never scale volume"

    def test_the_dormant_path_refuses_bad_source_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ca_module, "APPLY_DIVIDEND_ADJUSTMENT", True)

        with pytest.raises(AdjustmentError, match="close before the ex-date"):
            factors_for(CorporateActionType.DIVIDEND, dividend_amount=Decimal(25))

        with pytest.raises(AdjustmentError, match="bad source data"):
            factors_for(
                CorporateActionType.DIVIDEND,
                dividend_amount=Decimal(500),
                reference_close=Decimal(500),
            )

    async def test_a_split_and_a_consolidation_on_one_symbol_compose(
        self, session: AsyncSession, symbol_id: int
    ) -> None:
        """Mixed action types must combine, not overwrite each other."""
        await _seed_daily_bars(session, symbol_id, days=60)
        actions = CorporateActionRepository(session)
        early, late = SPLIT_DATE - dt.timedelta(days=30), SPLIT_DATE
        await actions.upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "action_type": "SPLIT",
                    "ex_date": early,
                    "ratio_from": Decimal(1),
                    "ratio_to": Decimal(2),
                    "source": "test",
                },
                {
                    "symbol_id": symbol_id,
                    "action_type": "CONSOLIDATION",
                    "ex_date": late,
                    "ratio_from": Decimal(4),
                    "ratio_to": Decimal(1),
                    "source": "test",
                },
            ]
        )
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await session.flush()

        oldest = (
            await session.execute(
                text(
                    "SELECT DISTINCT price_adj_factor FROM ohlcv "
                    "WHERE symbol_id = :s AND ts < CAST(:d AS date)"
                ),
                {"s": symbol_id, "d": early},
            )
        ).all()
        middle = (
            await session.execute(
                text(
                    "SELECT DISTINCT price_adj_factor FROM ohlcv WHERE symbol_id = :s "
                    "AND ts >= CAST(:lo AS date) AND ts < CAST(:hi AS date)"
                ),
                {"s": symbol_id, "lo": early, "hi": late},
            )
        ).all()

        # Before both: 1/2 * 4 = 2. Between them: only the consolidation, = 4.
        assert {Decimal(r[0]) for r in oldest} == {Decimal(2)}
        assert {Decimal(r[0]) for r in middle} == {Decimal(4)}

    async def test_an_action_dated_before_every_bar_changes_nothing(
        self, session: AsyncSession, symbol_id: int
    ) -> None:
        """Only bars strictly before the ex-date are adjusted."""
        await _seed_daily_bars(session, symbol_id)
        actions = CorporateActionRepository(session)
        await actions.upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "action_type": "SPLIT",
                    "ex_date": SPLIT_DATE - dt.timedelta(days=365),
                    "ratio_from": Decimal(1),
                    "ratio_to": Decimal(5),
                    "source": "test",
                }
            ]
        )
        await session.flush()
        await actions.recompute_factors(symbol_id)
        await session.flush()

        factors = await TestAgainstTheDatabase._factors(session, symbol_id)
        assert factors == {(Decimal(1), Decimal(1))}
