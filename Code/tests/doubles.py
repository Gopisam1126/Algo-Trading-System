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

from algotrader.broker.ratelimit import RateLimitConfig
from algotrader.common.config import CONFIRM_LIVE_VARIABLE, LIVE_ENVIRONMENT, AppConfig


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
