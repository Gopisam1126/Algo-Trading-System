"""E22-S01: what the risk engine guarantees, for ANY input - not for the inputs a
hand-written test happened to pick.

Every engine-level property here runs the PRODUCTION engine - all fourteen checks
and the sizer, built by ``build_risk_engine`` from the shipped configuration -
through its public entry point, ``RiskEngine.evaluate``. A property of a helper
is a property of the helper (ENGINEERING_STANDARD: test the entry point).

The guarantees, each falsifiable:

- an approval's risk is within the budget, its quantity a positive whole lot,
  and its stop on the protective side of the entry (tasks 1 and 3);
- an approval never takes a slot that is not free (task 2);
- an approval is never inside a no-trade window, outside the session, or with
  less runway to its square-off than configured (the "time exit" half of
  invariant 5), and every deadline the configuration can produce precedes the
  BROKER's published square-off (task 4);
- the sizer's quantity moves the right way with each input - and where it does
  not (capital), only for the one reason that is meant to break it (task 5).

Task 1's sizer-level form ("never rounds up") predates this file, in
tests/unit/test_sizer.py (E14-S07).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from algotrader.common.calendar import IST, MarketCalendar, load_holidays_with_status
from algotrader.common.config import MAX_EXIT_BUFFER_MINUTES, MIN_EXIT_BUFFER_MINUTES, AppConfig
from algotrader.common.enums import AIVerdict, Direction
from algotrader.common.metrics import reset_metrics_for_testing
from algotrader.common.models.trading import Recommendation, RiskDecision
from algotrader.execution.risk.context import RiskContext
from algotrader.execution.risk.factory import build_risk_engine, sizing_policy
from algotrader.execution.sizer import BELOW_MINIMUM, size_position

CODE = Path(__file__).resolve().parents[2]
CONFIG = AppConfig()
POLICY = sizing_policy(CONFIG)
DAY = dt.date(2026, 8, 25)  # a Tuesday, a trading day
CID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

#: Zerodha's published MIS auto square-off, read 20 Nov 2026 (support FAQ
#: "intraday auto square-off timings"). Hand-written, never read from the
#: calendar: a wrong constant must not agree with itself (ENG-020).
ZERODHA_SQUAREOFF = {"cas": dt.time(15, 12), "non_cas": dt.time(15, 25), "fno": dt.time(15, 26)}

SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def _calendar() -> MarketCalendar:
    status = load_holidays_with_status(str(CODE / "config" / "nse_holidays.yaml"))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


CALENDAR = _calendar()
ENGINE = build_risk_engine(CONFIG, calendar=CALENDAR)


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    reset_metrics_for_testing()


def ist(hour: int, minute: int) -> dt.datetime:
    return dt.datetime.combine(DAY, dt.time(hour, minute), tzinfo=IST).astimezone(dt.UTC)


def recommendation(*, entry: Decimal, direction: Direction, at: dt.datetime) -> Recommendation:
    long = direction is Direction.LONG
    return Recommendation(
        correlation_id=CID,
        symbol="INFY",
        strategy_id="orb_long_v1",
        direction=direction,
        trigger_price=entry,
        suggested_stop=entry * (Decimal("0.99") if long else Decimal("1.01")),
        timeframe_agreement=3,
        ai_confidence=Decimal("0.82"),
        ai_verdict=AIVerdict.CONFIRM,
        ai_rationale="property",
        emitted_at=at,
    )


def context(**overrides: object) -> RiskContext:
    """An approvable context: every risk input known and ample, the session
    calm. A property overrides what it varies; the rest says 'fine'."""
    now = overrides.get("now", ist(11, 0))
    assert isinstance(now, dt.datetime)
    base: dict[str, object] = {
        "kill_switch_active": False,
        "unhealthy_services": (),
        "realised_pnl_today": Decimal(0),
        "unrealised_pnl_today": Decimal(0),
        "charges_today": Decimal(0),
        "consecutive_losses": 0,
        "daily_loss_halted": False,
        "consecutive_loss_halted": False,
        "now": now,
        "squareoff_deadline": CALENDAR.squareoff_deadline(DAY, is_cas_stock=False),
        "capital": Decimal("500000"),
        "slots_total": 5,
        "slots_used": 0,
        "symbol_restrictions": (),
        "symbol_sector": "IT",
        "atr": Decimal("13.5"),
        "available_margin": Decimal("10000000"),
        "margin_per_share": Decimal("1"),
        "lot_size": 1,
        "open_positions": (),
        "correlations": {},
        "last_price": Decimal("1000"),
        "size_factor": Decimal(1),
    }
    base.update(overrides)
    return RiskContext(**base)  # type: ignore[arg-type]


def evaluate(
    entry: Decimal = Decimal("1000"), direction: Direction = Direction.LONG, **ctx: object
) -> RiskDecision:
    ctx.setdefault("last_price", entry)
    built = context(**ctx)
    return ENGINE.evaluate(recommendation(entry=entry, direction=direction, at=built.now), built)


# ---------------------------------------------------------------------------
# Controls: the properties below are implications on APPROVED decisions, and an
# implication over a generator that never approves is vacuously true.
# ---------------------------------------------------------------------------


def test_the_base_context_is_approved_long_and_short() -> None:
    for direction in Direction:
        decision = evaluate(direction=direction)
        assert decision.approved, decision.detail
        assert decision.sizing is not None and decision.sizing.quantity > 0


def test_the_generators_produce_approvals() -> None:
    """A sweep across the property ranges approves often - the properties are
    not satisfied by an engine that refuses everything."""
    approved = sum(
        evaluate(
            entry=Decimal(entry), atr=Decimal(atr), capital=Decimal(capital), direction=direction
        ).approved
        for entry in ("50", "800", "4000")
        for atr in ("0.5", "12", "90")
        for capital in ("200000", "5000000")
        for direction in Direction
    )
    assert approved >= 18, approved


# ---------------------------------------------------------------------------
# Tasks 1 and 3: an approval is bounded in risk and protected by its stop.
# ---------------------------------------------------------------------------

entries = st.decimals(min_value="10", max_value="20000", places=2)
atrs = st.decimals(min_value="0.05", max_value="400", places=4)
capitals = st.decimals(min_value="50000", max_value="50000000", places=2)
lots = st.sampled_from([1, 5, 25, 50, 100])
factors = st.sampled_from([Decimal("0.5"), Decimal(1)])


@SETTINGS
@given(
    entry=entries,
    atr=atrs,
    capital=capitals,
    lot=lots,
    factor=factors,
    direction=st.sampled_from(Direction),
)
def test_an_approval_risks_no_more_than_the_budget_and_has_a_protective_stop(
    entry: Decimal, atr: Decimal, capital: Decimal, lot: int, factor: Decimal, direction: Direction
) -> None:
    decision = evaluate(
        entry=entry, atr=atr, capital=capital, lot_size=lot, size_factor=factor, direction=direction
    )
    if not decision.approved:
        assert (
            decision.sizing is None or decision.sizing.quantity == 0 or decision.reason is not None
        )
        return
    sizing = decision.sizing
    assert sizing is not None
    assert sizing.quantity > 0 and sizing.quantity % lot == 0, "a positive whole lot"
    distance = abs(sizing.entry_price - sizing.stop_price)
    assert distance > 0 and sizing.stop_price > 0
    if direction is Direction.LONG:
        assert sizing.stop_price < sizing.entry_price, "a long's stop is below its entry"
    else:
        assert sizing.stop_price > sizing.entry_price, "a short's stop is above its entry"
    assert sizing.capital_at_risk == sizing.quantity * distance
    budget = capital * POLICY.risk_pct / 100
    assert sizing.capital_at_risk <= budget, (sizing.capital_at_risk, budget)


# ---------------------------------------------------------------------------
# Task 2: an approval never takes a slot that is not free.
# ---------------------------------------------------------------------------


@SETTINGS
@given(total=st.integers(min_value=1, max_value=20), data=st.data())
def test_an_approval_always_has_a_free_slot(total: int, data: st.DataObject) -> None:
    used = data.draw(st.integers(min_value=0, max_value=total), label="slots_used")
    decision = evaluate(slots_total=total, slots_used=used)
    if used >= total:
        assert not decision.approved, "a full book was approved"
    else:
        assert decision.approved, decision.detail  # control: a free slot is not refused


# ---------------------------------------------------------------------------
# Task 4: the time exit - an approval has runway, and the deadline precedes the
# broker's.
# ---------------------------------------------------------------------------


@SETTINGS
@given(
    minute=st.integers(min_value=8 * 60 + 30, max_value=15 * 60 + 45),
    cas=st.booleans(),
)
def test_an_approval_is_in_session_outside_no_trade_windows_and_has_runway(
    minute: int, cas: bool
) -> None:
    now = ist(minute // 60, minute % 60)
    deadline = CALENDAR.squareoff_deadline(DAY, is_cas_stock=cas)
    decision = evaluate(now=now, squareoff_deadline=deadline)
    if not decision.approved:
        return
    local = now.astimezone(IST).time()
    assert dt.time(9, 15) <= local < dt.time(15, 30), f"approved outside the session at {local}"
    for start, end in CONFIG.execution.no_trade_windows:
        assert not start <= local < end, (
            f"approved inside the no-trade window {start}-{end} at {local}"
        )
    runway = (deadline - now).total_seconds() / 60
    assert runway >= CONFIG.risk.min_minutes_to_squareoff, f"approved with {runway} minutes left"


def test_approvals_do_happen_mid_session() -> None:
    """Control for the property above."""
    assert evaluate(now=ist(11, 0)).approved
    assert not evaluate(now=ist(9, 17)).approved  # the 09:15-09:20 window
    assert not evaluate(now=ist(15, 1)).approved  # the closing window


trading_days = st.dates(min_value=dt.date(2026, 1, 1), max_value=dt.date(2026, 12, 31)).filter(
    CALENDAR.is_trading_day
)


@SETTINGS
@given(
    day=trading_days,
    buffer=st.integers(min_value=MIN_EXIT_BUFFER_MINUTES, max_value=MAX_EXIT_BUFFER_MINUTES),
    cls=st.sampled_from(["cas", "non_cas", "fno"]),
)
def test_every_deadline_the_configuration_allows_precedes_the_brokers(
    day: dt.date, buffer: int, cls: str
) -> None:
    """For every trading day of 2026, every buffer the configuration accepts and
    every stock class, our square-off is before Zerodha's - on both sides of the
    7 Dec 2026 closing-auction change."""
    AppConfig.model_validate({"risk": {"exit_buffer_minutes": buffer}})  # the config accepts it
    deadline = CALENDAR.squareoff_deadline(
        day, is_cas_stock=cls == "cas", is_fno=cls == "fno", buffer_minutes=buffer
    )
    broker = dt.datetime.combine(day, ZERODHA_SQUAREOFF[cls], tzinfo=IST)
    assert deadline < broker, (
        f"{cls} on {day} with buffer {buffer}: {deadline} is not before {broker}"
    )


def test_a_buffer_the_configuration_refuses_is_refused() -> None:
    """Control: the range above is the configuration's, so a buffer below it
    (the one that would move the deadline past the broker's) must not load."""
    with pytest.raises(ValueError, match="exit_buffer_minutes"):
        AppConfig.model_validate({"risk": {"exit_buffer_minutes": MIN_EXIT_BUFFER_MINUTES - 1}})


# ---------------------------------------------------------------------------
# Task 5: the sizer's quantity moves the right way with each input.
# ---------------------------------------------------------------------------


def quantity(entry: Decimal = Decimal("1000"), **ctx: object) -> int:
    ctx.setdefault("last_price", entry)
    built = context(**ctx)
    return size_position(
        recommendation(entry=entry, direction=Direction.LONG, at=built.now), built, POLICY
    ).quantity


def _pair(strategy: st.SearchStrategy[Decimal]) -> st.SearchStrategy[tuple[Decimal, Decimal]]:
    return st.tuples(strategy, strategy).map(sorted).map(tuple)  # type: ignore[arg-type]


margins = st.decimals(min_value="1000", max_value="100000000", places=2)


@SETTINGS
@given(pair=_pair(margins), atr=atrs)
def test_more_margin_never_means_fewer_shares(pair: tuple[Decimal, Decimal], atr: Decimal) -> None:
    low, high = pair
    assert quantity(available_margin=low, atr=atr) <= quantity(available_margin=high, atr=atr)


@SETTINGS
@given(pair=_pair(atrs))
def test_more_volatility_never_means_more_shares(pair: tuple[Decimal, Decimal]) -> None:
    low, high = pair
    assert quantity(atr=high) <= quantity(atr=low)


@SETTINGS
@given(pair=_pair(st.decimals(min_value="0.5", max_value="5000", places=2)), atr=atrs)
def test_a_costlier_margin_per_share_never_means_more_shares(
    pair: tuple[Decimal, Decimal], atr: Decimal
) -> None:
    low, high = pair
    assert quantity(margin_per_share=high, atr=atr) <= quantity(margin_per_share=low, atr=atr)


def sized(entry: Decimal = Decimal("1000"), **ctx: object):  # type: ignore[no-untyped-def]
    ctx.setdefault("last_price", entry)
    built = context(**ctx)
    return size_position(
        recommendation(entry=entry, direction=Direction.LONG, at=built.now), built, POLICY
    )


@SETTINGS
@given(pair=_pair(entries), atr=atrs)
def test_a_dearer_entry_means_more_shares_only_through_the_minimum_position_rule(
    pair: tuple[Decimal, Decimal], atr: Decimal
) -> None:
    """Found by this property on its first run (E22-S01): entry 10.00 with ATR
    3.3334 sized 0 and entry 10.02 sized 999. The risk budget allows 999 shares
    at either price; at 10.00 they are worth 9,990, under the 10,000 minimum
    position (RSK-012, a token position is refused), and at 10.02 they are not.
    Its second run found the other floor: at 10.00 with ATR 6.6667 the 10.0
    stop distance reaches the price itself (a long's stop at zero), and the
    sizer refuses - correctly - while 20.00 sizes. So a dearer entry may size
    MORE shares only when the cheaper one was REFUSED by a price floor (the
    minimum position, or a stop beyond zero); every clamp itself shrinks with
    price."""
    low, high = pair
    cheap, dear = sized(entry=low, atr=atr), sized(entry=high, atr=atr)
    if dear.quantity > cheap.quantity:
        floor = cheap.binding_constraint == BELOW_MINIMUM or cheap.binding_constraint.endswith(
            "no position is sizeable"
        )
        assert cheap.quantity == 0 and floor, (cheap, dear)


def test_the_price_exception_exists() -> None:
    """Control: the counterexample hypothesis found, pinned."""
    cheap = sized(entry=Decimal("10.00"), atr=Decimal("3.3334"))
    dear = sized(entry=Decimal("10.02"), atr=Decimal("3.3334"))
    assert cheap.quantity == 0 and cheap.binding_constraint == BELOW_MINIMUM
    assert dear.quantity == 999


@SETTINGS
@given(atr=atrs)
def test_a_reduced_size_factor_never_means_more_shares(atr: Decimal) -> None:
    assert quantity(size_factor=Decimal("0.5"), atr=atr) <= quantity(
        size_factor=Decimal(1), atr=atr
    )


@SETTINGS
@given(pair=_pair(capitals), atr=atrs, margin=margins)
def test_more_capital_means_fewer_shares_only_through_the_minimum_position_rule(
    pair: tuple[Decimal, Decimal], atr: Decimal, margin: Decimal
) -> None:
    """NOT monotonic, by design: the margin cap does not grow with capital, while
    the minimum position (RSK-012, a token position is refused) does - so the
    same margin-bound quantity can be a position at 2 lakh and a token at 2
    crore. That is the only way more capital may size fewer shares."""
    low, high = pair
    small = quantity(capital=low, atr=atr, available_margin=margin)
    built = context(capital=high, atr=atr, available_margin=margin, last_price=Decimal("1000"))
    large = size_position(
        recommendation(entry=Decimal("1000"), direction=Direction.LONG, at=built.now), built, POLICY
    )
    if large.quantity < small:
        assert large.quantity == 0 and large.binding_constraint == BELOW_MINIMUM, large


def test_the_capital_exception_exists() -> None:
    """Control: the exception above is real, not a hypothetical the property
    tolerates for nothing."""
    # Margin caps the position at 100,000 / 400 = 250 shares. At 2 lakh the risk
    # budget binds first (98 shares); at 2 crore the 250 margin-bound shares are
    # worth 2.5 lakh, under the 2% minimum of 4 lakh.
    margin = {"available_margin": Decimal("100000"), "margin_per_share": Decimal("400")}
    small = quantity(capital=Decimal("200000"), **margin)
    built = context(capital=Decimal("20000000"), last_price=Decimal("1000"), **margin)
    large = size_position(
        recommendation(entry=Decimal("1000"), direction=Direction.LONG, at=built.now), built, POLICY
    )
    assert small > 0 and large.quantity == 0 and large.binding_constraint == BELOW_MINIMUM
