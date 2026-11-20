"""Shared test doubles for shapes the 10 Nov 2026 audit remediation made required.

Kept deliberately small. Each double is as permissive as the real thing in the
one respect a test is NOT about, and no more - a double more capable than the
real component proves capabilities the system does not have (QA-E15-24).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from algotrader.broker.adapter import Venue
from algotrader.broker.ratelimit import RateLimitConfig
from algotrader.common.config import CONFIRM_LIVE_VARIABLE, LIVE_ENVIRONMENT, AppConfig


def on_venue(venue: Venue, *doubles: Any) -> None:
    """Declare that each double stands for an account on ``venue`` (audit NR-002).

    The execution service refuses collaborators whose venue is not its mode's,
    and an object that declares none counts as LIVE. A double is a simulation,
    but which ACCOUNT it stands for is the test's statement, so it is set
    here, explicitly, rather than defaulted on the class.
    """
    for double in doubles:
        double.venue = venue


#: NSE's capital-market tick by price band (audit NR-006), effective from the
#: trade date of 15 Apr 2025 - NSE circular NSE/CMTR/67133, as published in
#: Zerodha's bulletin 408151 of 17 Mar 2025 (read 20 Nov 2026; NSE's own PDF
#: does not load from this host, B8). The upper bound of each band, inclusive,
#: and its tick; above the last bound the tick is NSE_TOP_TICK. NSE reviews it
#: monthly on the close of the month's last trading day, so an instrument's
#: tick is fixed for the month whatever its price does intraday. The tests
#: hard-coded 0.05 for Rs 1,200 stocks, whose tick has been 0.10 since then.
NSE_EQUITY_TICK_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("250"), Decimal("0.01")),  # "Below 250": the bound is EXCLUSIVE here
    (Decimal("1000"), Decimal("0.05")),  # ">= 250 to 1000"
    (Decimal("5000"), Decimal("0.10")),  # "> 1000 to 5000"
    (Decimal("10000"), Decimal("0.50")),  # "> 5000 to 10000"
    (Decimal("20000"), Decimal("1")),  # "> 10000 to 20000"
)
NSE_TOP_TICK = Decimal("5")  # "> 20000"


def nse_equity_tick(reference_close: Decimal) -> Decimal:
    """The tick NSE assigns a stock whose last month-end close was ``reference_close``."""
    if reference_close <= 0:
        raise ValueError(f"a close is positive, not {reference_close}")
    (below, lowest), *rest = NSE_EQUITY_TICK_BANDS
    if reference_close < below:
        return lowest
    for upper, tick in rest:
        if reference_close <= upper:
            return tick
    return NSE_TOP_TICK


class Unreachable:
    """A collaborator a test never calls, which still has to declare a venue."""

    def __init__(self, venue: Venue) -> None:
        self.venue = venue

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"this test was not meant to reach .{name}")


class CountingLimiter:
    """A ``BrokerRateLimiter`` that admits every call and counts them.

    ``KiteTradingAdapter`` requires a limiter (audit RSK-010). A test about
    order MAPPING is not about rate limiting, so this admits everything - and
    records each token taken, so a test that IS about the budget can assert it.
    The real limiter's behaviour is covered against Redis in
    ``tests/integration/test_broker_ratelimit.py``.
    """

    def __init__(self) -> None:
        self.orders = 0
        self.data = 0
        self.config = RateLimitConfig()

    async def acquire_order(self, *, wait_seconds: float = 0.0) -> float:
        self.orders += 1
        return float(self.config.order_burst)

    async def acquire_data(self) -> float:
        self.data += 1
        return float(self.config.data_burst)


def live_config(monkeypatch: pytest.MonkeyPatch, raw: dict[str, Any]) -> AppConfig:
    """A LIVE ``AppConfig`` loaded the only way one can be (audit RSK-008).

    Live mode needs ``ALGOTRADER_ENV=production`` and ``ALGOTRADER_CONFIRM_LIVE``
    equal to the configuration's own hash. The hash is computed from an
    unvalidated copy - ``model_copy`` does not run validators, and no validator
    transforms a value, so the copy hashes exactly as the validated live config
    will - and then the real load runs with both variables set.
    """
    # Audit LEG-001: live needs the broker's written answer on algo tagging.
    # Supplied here unless the test is about it.
    broker = {"algo_tagging_confirmation": "Zerodha support ticket TEST-1, 16 Nov 2026"}
    raw = {**raw, "broker": {**broker, **raw.get("broker", {})}}
    paper = AppConfig.model_validate({**raw, "system": {**raw["system"], "mode": "paper"}})
    candidate = paper.model_copy(
        update={"system": paper.system.model_copy(update={"mode": raw["system"]["mode"]})}
    )
    monkeypatch.setenv("ALGOTRADER_ENV", LIVE_ENVIRONMENT)
    monkeypatch.setenv(CONFIRM_LIVE_VARIABLE, candidate.config_hash())
    return AppConfig.model_validate(raw)


class RegisteredStrategies:
    """The strategy table's one answer the execution service reads: a state.

    ``ExecutionService`` refuses a signal whose strategy is not in a state that
    places orders (audit RSK-006), reading the state through
    ``RepositoryContextStore.strategy_state`` -> ``strategy_by_id``. A wiring
    test about orders and fills registers its strategies here rather than
    building full DSL documents; the registry's own persistence is tested
    against Postgres in ``tests/integration/test_strategy_registry_db.py``.
    """

    def __init__(self, states: dict[str, str]) -> None:
        self.states = dict(states)

    async def strategy_by_id(self, strategy_id: str) -> dict[str, Any] | None:
        state = self.states.get(strategy_id)
        return None if state is None else {"strategy_id": strategy_id, "state": state}


class MarketAt:
    """A live-price producer that answers one price for every symbol.

    For system tests whose recommendations all trigger at one price and which
    publish no quote state: the market agrees with the trigger, so the RSK-002
    divergence guard passes and the test stays about what it is about. The real
    producer, ``execution.live_inputs.LiveQuotePrice``, is unit-tested on its
    own (fresh, stale, future-dated, absent and non-positive quotes).
    """

    def __init__(self, price: Decimal) -> None:
        self.price = price

    async def last_price(self, symbol: str, *, now: dt.datetime) -> Decimal | None:
        return self.price
