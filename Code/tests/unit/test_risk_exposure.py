"""Portfolio exposure checks 8–10 (E14-S04) — one class per acceptance criterion.

AC1  correlated to too many open positions rejects, detail names them
AC2  a held position with NO correlation entry rejects — unknown is not zero
AC3  an empty book clears the correlation guard vacuously
AC4  sector already at the cap rejects, detail gives current vs cap
AC5  an unknown sector — candidate or held position — rejects
AC6  net directional exposure already at the cap rejects
AC7  longs and shorts net off
AC8  the CONTROL — flat and diversified books pass all three
AC9  registration order, running after eligibility
AC10 the original criterion: four PSU banks cannot occupy four slots

AC8 is the one that makes the rest mean anything, and AC10 is the one the
story was actually written to get.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

import pytest

from algotrader.common.enums import AIVerdict, Direction, RejectReason
from algotrader.common.metrics import reset_metrics_for_testing
from algotrader.common.models.trading import Recommendation
from algotrader.execution.risk.checks import (
    EXPOSURE_ORDER,
    MAX_SYMBOLS_NAMED,
    build_correlation_check,
    build_exposure_checks,
    build_net_exposure_check,
    build_sector_exposure_check,
)
from algotrader.execution.risk.context import OpenPosition, RiskContext
from algotrader.execution.risk.framework import MAX_CHECK_ID, RiskEngine

MIDSESSION = dt.datetime(2026, 8, 25, 4, 30, tzinfo=dt.UTC)
DEADLINE = dt.datetime(2026, 8, 25, 9, 40, tzinfo=dt.UTC)
CID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
CAPITAL = Decimal("500000")

#: What system.yaml configures.
MAX_CORRELATED = 2
THRESHOLD = Decimal("0.7")
MAX_SECTOR_PCT = Decimal("40")
MAX_NET_PCT = Decimal("60")


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    reset_metrics_for_testing()


def _rec(symbol: str = "SBIN", direction: Direction = Direction.LONG) -> Recommendation:
    long = direction is Direction.LONG
    return Recommendation(
        correlation_id=CID,
        symbol=symbol,
        strategy_id="orb_long_v1",
        direction=direction,
        trigger_price=Decimal("1200.00"),
        suggested_stop=Decimal("1186.45") if long else Decimal("1213.55"),
        timeframe_agreement=3,
        ai_confidence=Decimal("0.82"),
        ai_verdict=AIVerdict.CONFIRM,
        ai_rationale="probe",
        emitted_at=MIDSESSION,
    )


def _pos(
    symbol: str,
    *,
    notional: str = "50000",
    sector: str | None = "PSU_BANK",
    direction: Direction = Direction.LONG,
) -> OpenPosition:
    """A position of a stated notional. Quantity is derived so the arithmetic
    the checks do is on a number the test names, not one it has to work out."""
    price = Decimal("100")
    return OpenPosition(
        symbol=symbol,
        direction=direction,
        quantity=int(Decimal(notional) / price),
        entry_price=price,
        stop_price=Decimal("95") if direction is Direction.LONG else Decimal("105"),
        sector=sector,
    )


def _ctx(**overrides) -> RiskContext:
    base: dict = {
        "now": MIDSESSION,
        "squareoff_deadline": DEADLINE,
        "capital": CAPITAL,
        "slots_total": 5,
        "slots_used": 0,
        "symbol_sector": "PSU_BANK",
    }
    base.update(overrides)
    return RiskContext(**base)


def _correlation():
    return build_correlation_check(MAX_CORRELATED, THRESHOLD).fn


def _sector():
    return build_sector_exposure_check(MAX_SECTOR_PCT).fn


def _net():
    return build_net_exposure_check(MAX_NET_PCT).fn


def _all_three():
    return build_exposure_checks(
        max_correlated_positions=MAX_CORRELATED,
        correlation_threshold=THRESHOLD,
        max_sector_exposure_pct=MAX_SECTOR_PCT,
        max_net_directional_exposure_pct=MAX_NET_PCT,
    )


class TestAC1CorrelationLimit:
    def test_too_many_correlated_names_rejects(self) -> None:
        ctx = _ctx(
            open_positions=(_pos("PNB"), _pos("BANKBARODA")),
            correlations={"PNB": Decimal("0.85"), "BANKBARODA": Decimal("0.79")},
        )
        outcome = _correlation()(_rec(), ctx)
        assert not outcome.passed
        assert outcome.reason is RejectReason.CORRELATION_LIMIT

    def test_one_correlated_name_passes(self) -> None:
        """The limit is 2, so one is fine. Without this the check could be
        rejecting on 'any correlation at all' and AC1 would not notice."""
        ctx = _ctx(
            open_positions=(_pos("PNB"), _pos("TCS", sector="IT")),
            correlations={"PNB": Decimal("0.85"), "TCS": Decimal("0.10")},
        )
        assert _correlation()(_rec(), ctx).passed

    def test_the_detail_names_the_correlated_symbols(self) -> None:
        ctx = _ctx(
            open_positions=(_pos("PNB"), _pos("BANKBARODA")),
            correlations={"PNB": Decimal("0.85"), "BANKBARODA": Decimal("0.79")},
        )
        detail = _correlation()(_rec(), ctx).detail
        assert "PNB" in detail
        assert "BANKBARODA" in detail

    def test_negative_correlation_counts_as_correlated(self) -> None:
        """-0.85 is as much one bet as +0.85. A long in one and a short in the
        other is a single spread position that fails together when the
        relationship breaks, not two independent trades."""
        ctx = _ctx(
            open_positions=(_pos("PNB"), _pos("BANKBARODA")),
            correlations={"PNB": Decimal("-0.85"), "BANKBARODA": Decimal("-0.90")},
        )
        assert not _correlation()(_rec(), ctx).passed

    def test_the_threshold_boundary_is_inclusive(self) -> None:
        at = _ctx(
            open_positions=(_pos("PNB"), _pos("BANKBARODA")),
            correlations={"PNB": THRESHOLD, "BANKBARODA": THRESHOLD},
        )
        assert not _correlation()(_rec(), at).passed
        below = _ctx(
            open_positions=(_pos("PNB"), _pos("BANKBARODA")),
            correlations={
                "PNB": THRESHOLD - Decimal("0.01"),
                "BANKBARODA": THRESHOLD - Decimal("0.01"),
            },
        )
        assert _correlation()(_rec(), below).passed

    def test_a_flood_of_correlated_names_does_not_flood_the_detail(self) -> None:
        positions = tuple(_pos(f"BANK{i}", notional="1000") for i in range(200))
        ctx = _ctx(
            open_positions=positions,
            correlations={f"BANK{i}": Decimal("0.9") for i in range(200)},
        )
        detail = _correlation()(_rec(), ctx).detail
        assert len(detail) < 512
        assert f"and {200 - MAX_SYMBOLS_NAMED} more" in detail

    def test_a_nonsensical_limit_is_refused_at_construction(self) -> None:
        """max_correlated_positions of 0 would reject every candidate the
        moment any position is open — a configuration that can never trade."""
        with pytest.raises(ValueError, match="below 1"):
            build_correlation_check(0, THRESHOLD)

    @pytest.mark.parametrize("bad", [Decimal("0"), Decimal("1.5"), Decimal("-0.5")])
    def test_a_nonsensical_threshold_is_refused_at_construction(self, bad: Decimal) -> None:
        with pytest.raises(ValueError, match="outside"):
            build_correlation_check(MAX_CORRELATED, bad)


class TestAC2UnknownCorrelationIsARejection:
    """ "We could not measure it" is not "they are unrelated". The second is a
    claim; the first is the absence of one, and only one of them is safe."""

    def test_a_held_position_with_no_correlation_entry_raises(self) -> None:
        ctx = _ctx(open_positions=(_pos("PNB"),), correlations={})
        with pytest.raises(ValueError, match="PNB"):
            _correlation()(_rec(), ctx)

    def test_the_engine_turns_that_into_a_fault_not_an_approval(self) -> None:
        engine = RiskEngine(checks=[build_correlation_check(MAX_CORRELATED, THRESHOLD)])
        decision = engine.evaluate(_rec(), _ctx(open_positions=(_pos("PNB"),), correlations={}))
        assert not decision.approved
        assert decision.reason is RejectReason.RISK_ENGINE_FAULT

    def test_unknown_is_distinguishable_from_a_real_correlation_rejection(self) -> None:
        """SIT-001's distinction at a new site: a missing matrix means a
        pre-market job failed, and a correlated book means the guard worked.
        An operator responds to those differently."""
        engine = RiskEngine(checks=[build_correlation_check(MAX_CORRELATED, THRESHOLD)])
        unknown = engine.evaluate(_rec(), _ctx(open_positions=(_pos("PNB"),), correlations={}))
        limited = engine.evaluate(
            _rec(),
            _ctx(
                open_positions=(_pos("PNB"), _pos("BANKBARODA")),
                correlations={"PNB": Decimal("0.9"), "BANKBARODA": Decimal("0.9")},
            ),
        )
        assert limited.reason is RejectReason.CORRELATION_LIMIT
        assert unknown.reason is RejectReason.RISK_ENGINE_FAULT

    def test_one_missing_entry_among_many_is_enough_to_refuse(self) -> None:
        """Partial knowledge is not knowledge. Evaluating the guard against the
        subset we happen to have would silently apply a weaker limit."""
        ctx = _ctx(
            open_positions=(_pos("PNB"), _pos("BANKBARODA")),
            correlations={"PNB": Decimal("0.1")},
        )
        with pytest.raises(ValueError, match="BANKBARODA"):
            _correlation()(_rec(), ctx)


class TestAC3AnEmptyBookIsVacuouslyFine:
    def test_no_open_positions_passes_with_no_correlations(self) -> None:
        assert _correlation()(_rec(), _ctx(open_positions=(), correlations={})).passed


class TestAC4SectorExposureCap:
    def test_a_sector_at_the_cap_rejects(self) -> None:
        # 40% of 500000 = 200000.
        ctx = _ctx(open_positions=(_pos("PNB", notional="200000"),))
        outcome = _sector()(_rec(), ctx)
        assert not outcome.passed
        assert outcome.reason is RejectReason.SECTOR_EXPOSURE_LIMIT

    def test_a_sector_below_the_cap_passes(self) -> None:
        ctx = _ctx(open_positions=(_pos("PNB", notional="199999"),))
        assert _sector()(_rec(), ctx).passed

    def test_the_detail_gives_current_against_the_cap(self) -> None:
        ctx = _ctx(open_positions=(_pos("PNB", notional="250000"),))
        detail = _sector()(_rec(), ctx).detail
        assert "50.0%" in detail
        assert "40" in detail
        assert "PSU_BANK" in detail

    def test_only_the_candidates_own_sector_counts(self) -> None:
        """The control that keeps this from being a gross-exposure check in
        disguise. A large IT book must not block a PSU bank."""
        ctx = _ctx(
            symbol_sector="PSU_BANK",
            open_positions=(
                _pos("TCS", notional="200000", sector="IT"),
                _pos("INFY", notional="200000", sector="IT"),
            ),
        )
        assert _sector()(_rec(), ctx).passed

    @pytest.mark.parametrize("bad", [Decimal("0"), Decimal("101"), Decimal("-5")])
    def test_a_nonsensical_cap_is_refused_at_construction(self, bad: Decimal) -> None:
        with pytest.raises(ValueError, match="outside"):
            build_sector_exposure_check(bad)


class TestAC5AnUnknownSectorIsARejection:
    """Both sides of the same hole. `RiskContext.sector_exposure` used to take
    `str | None` and return Decimal(0) for None, so an unclassified instrument
    sailed past the PRIMARY concentration control."""

    def test_an_unclassified_candidate_raises(self) -> None:
        with pytest.raises(ValueError, match="no sector classification"):
            _sector()(_rec(), _ctx(symbol_sector=None))

    def test_an_unclassified_open_position_raises(self) -> None:
        """The subtler direction: a held position with sector=None matches no
        sector, so its notional escapes every total and the cap never binds."""
        ctx = _ctx(open_positions=(_pos("PNB", notional="300000", sector=None),))
        with pytest.raises(ValueError, match="no sector"):
            _sector()(_rec(), ctx)

    def test_the_error_names_the_unclassified_position(self) -> None:
        ctx = _ctx(open_positions=(_pos("MYSTERY", sector=None),))
        with pytest.raises(ValueError, match="MYSTERY"):
            _sector()(_rec(), ctx)

    def test_the_helper_no_longer_accepts_none_at_all(self) -> None:
        """Structural, not incidental. The signature is the thing that stops
        the next caller from reintroducing the zero."""
        ctx = _ctx(open_positions=(_pos("PNB", notional="100000"),))
        assert ctx.sector_exposure("PSU_BANK") == Decimal("100000")
        assert ctx.positions_missing_a_sector() == ()

    def test_a_fully_classified_book_is_the_control(self) -> None:
        ctx = _ctx(open_positions=(_pos("PNB", notional="1000"),))
        assert _sector()(_rec(), ctx).passed


class TestAC6NetDirectionalExposureCap:
    def test_a_one_sided_book_at_the_cap_rejects(self) -> None:
        # 60% of 500000 = 300000, all long.
        ctx = _ctx(open_positions=(_pos("PNB", notional="300000"),))
        outcome = _net()(_rec(), ctx)
        assert not outcome.passed
        assert outcome.reason is RejectReason.NET_EXPOSURE_LIMIT

    def test_below_the_cap_passes(self) -> None:
        ctx = _ctx(open_positions=(_pos("PNB", notional="299999"),))
        assert _net()(_rec(), ctx).passed

    def test_a_short_book_is_capped_the_same_way(self) -> None:
        """abs(), not the raw signed value — otherwise a heavily short book
        would read as -60% and pass a `>= 60` test."""
        ctx = _ctx(open_positions=(_pos("PNB", notional="300000", direction=Direction.SHORT),))
        outcome = _net()(_rec(), ctx)
        assert not outcome.passed
        assert "short" in outcome.detail

    @pytest.mark.parametrize("bad", [Decimal("0"), Decimal("101")])
    def test_a_nonsensical_cap_is_refused_at_construction(self, bad: Decimal) -> None:
        with pytest.raises(ValueError, match="outside"):
            build_net_exposure_check(bad)


class TestAC7LongsAndShortsNetOff:
    def test_a_matched_book_has_no_net_exposure(self) -> None:
        """A long and a short of equal notional is not a directional bet, and
        this is the check that measures direction. Reading gross here would
        refuse a hedged book for being large rather than one-sided."""
        ctx = _ctx(
            open_positions=(
                _pos("PNB", notional="300000"),
                _pos("TCS", notional="300000", sector="IT", direction=Direction.SHORT),
            )
        )
        assert ctx.net_exposure() == Decimal(0)
        assert _net()(_rec(), ctx).passed

    def test_gross_and_net_differ_and_this_check_reads_net(self) -> None:
        ctx = _ctx(
            open_positions=(
                _pos("PNB", notional="300000"),
                _pos("TCS", notional="300000", sector="IT", direction=Direction.SHORT),
            )
        )
        assert ctx.gross_exposure() == Decimal("600000")
        assert ctx.net_exposure() == Decimal(0)


class TestAC8TheControl:
    """Three checks that rejected everything would satisfy AC1–AC7 perfectly."""

    def test_a_flat_book_passes_all_three(self) -> None:
        engine = RiskEngine(checks=_all_three())
        decision = engine.evaluate(_rec(), _ctx(open_positions=(), correlations={}))
        assert decision.checks_passed == list(EXPOSURE_ORDER)

    def test_a_diversified_book_below_every_cap_passes_all_three(self) -> None:
        """The realistic case. If this failed, the system would never take a
        second position and no per-check test would say why."""
        engine = RiskEngine(checks=_all_three())
        ctx = _ctx(
            symbol_sector="PSU_BANK",
            open_positions=(
                _pos("TCS", notional="50000", sector="IT"),
                _pos("RELIANCE", notional="50000", sector="ENERGY"),
            ),
            correlations={"TCS": Decimal("0.1"), "RELIANCE": Decimal("0.2")},
        )
        assert engine.evaluate(_rec(), ctx).checks_passed == list(EXPOSURE_ORDER)


class TestAC9OrderAndRegistration:
    def test_the_declared_order(self) -> None:
        assert EXPOSURE_ORDER == ("correlation", "sector_exposure", "net_exposure")
        assert tuple(c.id for c in _all_three()) == EXPOSURE_ORDER

    def test_every_check_id_fits_the_audit_column(self) -> None:
        for check in _all_three():
            assert len(check.id) <= MAX_CHECK_ID, check.id

    def test_every_check_carries_a_description(self) -> None:
        for check in _all_three():
            assert check.description

    def test_the_factory_is_keyword_only(self) -> None:
        """Four numeric limits in a row is exactly the signature where a
        positional call silently swaps two and nothing complains."""
        with pytest.raises(TypeError):
            build_exposure_checks(2, Decimal("0.7"), Decimal("40"), Decimal("60"))  # type: ignore[misc]


class TestAC10FourPSUBanksCannotOccupyFourSlots:
    """The story's original acceptance criterion, end to end.

    Worth its own class because it is the only one stated in terms of an
    outcome someone cared about rather than a mechanism.
    """

    def test_the_fourth_psu_bank_is_refused(self) -> None:
        engine = RiskEngine(checks=_all_three())
        held = (
            _pos("PNB", notional="60000"),
            _pos("BANKBARODA", notional="60000"),
            _pos("CANBK", notional="60000"),
        )
        ctx = _ctx(
            symbol_sector="PSU_BANK",
            slots_used=3,
            open_positions=held,
            correlations={
                "PNB": Decimal("0.88"),
                "BANKBARODA": Decimal("0.84"),
                "CANBK": Decimal("0.81"),
            },
        )
        decision = engine.evaluate(_rec("SBIN"), ctx)
        assert not decision.approved
        assert decision.reason is RejectReason.CORRELATION_LIMIT

    def test_the_third_is_already_refused(self) -> None:
        """Stronger than the criterion asks. With the limit at 2, the cluster
        caps at two names, so four never becomes reachable."""
        engine = RiskEngine(checks=_all_three())
        ctx = _ctx(
            symbol_sector="PSU_BANK",
            slots_used=2,
            open_positions=(_pos("PNB", notional="60000"), _pos("BANKBARODA", notional="60000")),
            correlations={"PNB": Decimal("0.88"), "BANKBARODA": Decimal("0.84")},
        )
        assert engine.evaluate(_rec("SBIN"), ctx).reason is RejectReason.CORRELATION_LIMIT

    def test_the_sector_cap_catches_what_correlation_misses(self) -> None:
        """The build concern made concrete: four PSU banks that correlate only
        MODERATELY are still one bet, and the correlation guard lets them
        through. Sector is the primary control precisely for this."""
        engine = RiskEngine(checks=_all_three())
        moderate = {"PNB": Decimal("0.45"), "BANKBARODA": Decimal("0.40"), "CANBK": Decimal("0.35")}
        ctx = _ctx(
            symbol_sector="PSU_BANK",
            slots_used=3,
            open_positions=(
                _pos("PNB", notional="70000"),
                _pos("BANKBARODA", notional="70000"),
                _pos("CANBK", notional="70000"),
            ),
            correlations=moderate,
        )
        decision = engine.evaluate(_rec("SBIN"), ctx)
        assert decision.reason is RejectReason.SECTOR_EXPOSURE_LIMIT, (
            "correlation was below the threshold, so only the sector cap could "
            "have caught this — which is why it is the primary control"
        )

    def test_four_unrelated_names_are_allowed(self) -> None:
        """The control for AC10, and the one that stops the guard from simply
        capping the book at three positions."""
        engine = RiskEngine(checks=_all_three())
        ctx = _ctx(
            symbol_sector="PHARMA",
            slots_used=3,
            open_positions=(
                _pos("TCS", notional="50000", sector="IT"),
                _pos("RELIANCE", notional="50000", sector="ENERGY"),
                _pos("MARUTI", notional="50000", sector="AUTO"),
            ),
            correlations={
                "TCS": Decimal("0.10"),
                "RELIANCE": Decimal("0.15"),
                "MARUTI": Decimal("0.05"),
            },
        )
        assert engine.evaluate(_rec("SUNPHARMA"), ctx).checks_passed == list(EXPOSURE_ORDER)


class TestTheChecksStayPure:
    def test_they_are_deterministic(self) -> None:
        ctx = _ctx(
            open_positions=(_pos("PNB"), _pos("BANKBARODA")),
            correlations={"PNB": Decimal("0.85"), "BANKBARODA": Decimal("0.79")},
        )
        first = [c.fn(_rec(), ctx) for c in _all_three()]
        second = [c.fn(_rec(), ctx) for c in _all_three()]
        assert [(o.passed, o.reason, o.detail) for o in first] == [
            (o.passed, o.reason, o.detail) for o in second
        ]

    def test_none_of_them_mutate_the_context(self) -> None:
        ctx = _ctx(
            open_positions=(_pos("PNB"), _pos("BANKBARODA")),
            correlations={"PNB": Decimal("0.85"), "BANKBARODA": Decimal("0.79")},
        )
        before = (ctx.open_positions, dict(ctx.correlations), ctx.symbol_sector)
        for check in _all_three():
            try:
                check.fn(_rec(), ctx)
            except ValueError:
                pass
        assert (ctx.open_positions, dict(ctx.correlations), ctx.symbol_sector) == before
