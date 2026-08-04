"""Broker profile tests.

The binding order-rate constraint is min(SEBI cap, broker API limit).  Zerodha
permits roughly a third of what Angel One does, so a config carried over
between brokers is actively wrong — these tests make sure that is caught at
startup rather than by the broker throttling us mid-session.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from algotrader.broker.profiles import (
    PROFILES,
    ZERODHA,
    AuthFlow,
    get_profile,
)
from algotrader.common.config import MAX_ORDERS_PER_SECOND, AppConfig


class TestProfiles:
    def test_zerodha_registered(self) -> None:
        assert get_profile("zerodha") is ZERODHA

    def test_unknown_broker_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown broker"):
            get_profile("some_random_broker")

    def test_zerodha_matches_sebi_threshold(self) -> None:
        """Zerodha enforces 10 OPS account-wide, matching SEBI's threshold."""
        assert ZERODHA.max_orders_per_second == 10

    def test_effective_rate_takes_the_minimum(self) -> None:
        """Our own cap is tighter than the broker's, so ours binds."""
        assert ZERODHA.effective_order_rate(MAX_ORDERS_PER_SECOND) == MAX_ORDERS_PER_SECOND

    def test_every_profile_has_a_positive_rate(self) -> None:
        for key, profile in PROFILES.items():
            assert profile.max_orders_per_second > 0, key

    def test_no_profile_exceeds_sebi_threshold(self) -> None:
        """SEBI's registration threshold is 10 orders/sec per segment."""
        for key, profile in PROFILES.items():
            assert profile.max_orders_per_second <= 10, key


class TestZerodhaOperationalCharacteristics:
    """These drive real design decisions, so pin them down."""

    def test_uses_redirect_auth_not_direct_login(self) -> None:
        assert ZERODHA.auth_flow is AuthFlow.REDIRECT_REQUEST_TOKEN

    def test_requires_manual_daily_login(self) -> None:
        """This sets a floor on how unattended the system can be.

        SEBI requires a fresh session before each pre-open, and Zerodha's
        flow needs a browser. Fully autonomous operation therefore needs
        either a solved login step or a human at ~07:00 each trading day.
        """
        assert ZERODHA.requires_manual_daily_login is True

    def test_has_documented_caveats(self) -> None:
        assert len(ZERODHA.caveats) >= 3


class TestConfigEnforcesBrokerLimit:
    def test_within_broker_limit_is_accepted(self) -> None:
        cfg = AppConfig.model_validate({
            "broker": {"primary": "zerodha"},
            "execution": {"max_orders_per_second": 3},
        })
        assert cfg.execution.max_orders_per_second == 3

    def test_our_own_hard_cap_still_binds(self) -> None:
        """Even where the broker allows 10, our code cap of 5 wins."""
        with pytest.raises(ValidationError, match="exceeds the hard cap"):
            AppConfig.model_validate({
                "broker": {"primary": "zerodha"},
                "execution": {"max_orders_per_second": 8},
            })

    def test_unknown_broker_in_config_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown broker"):
            AppConfig.model_validate({"broker": {"primary": "not_a_broker"}})

    def test_shipped_config_is_consistent(self) -> None:
        """The checked-in config must satisfy its own broker's limit."""
        from algotrader.common.config import load_config

        cfg = load_config()
        profile = get_profile(cfg.broker.primary)
        assert cfg.execution.max_orders_per_second <= profile.max_orders_per_second
