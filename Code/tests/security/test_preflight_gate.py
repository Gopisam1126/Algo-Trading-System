"""`make doctor` is the gate before real capital — so the gate itself is tested.

Every check here has the same shape of failure: the gate reports green while the
thing it claims to verify is absent or wrong. That is worse than having no gate,
because it converts an unknown into a false assurance.

The live-mode branches are the ones that matter and the ones nobody runs. A
developer sees PAPER mode every day; the LIVE path executes once, on the morning
real money is first at risk. These tests exercise it now instead.

Two real defects motivated this file, both found by QA rather than review:

- ``check_secrets`` hardcoded ``ANGELONE_*`` while ``broker.primary`` was
  ``zerodha``. In live mode it would have hard-failed on credentials this
  deployment never uses, and said nothing about the Kite credentials it does.
- ``check_market_calendar`` only ever warned about an unverified holiday list,
  including in live mode, despite BR-20 requiring it to block. The system could
  have gone live treating Diwali as a trading day.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from algotrader.broker.profiles import PROFILES, get_profile
from algotrader.common import calendar as calendar_mod
from algotrader.common.enums import SystemMode

CODE_ROOT = Path(__file__).resolve().parents[2]


def _load_doctor() -> Any:
    """Import scripts/doctor.py, which is not on the package path."""
    spec = importlib.util.spec_from_file_location("doctor", CODE_ROOT / "scripts" / "doctor.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["doctor"] = module
    spec.loader.exec_module(module)
    return module


doctor = _load_doctor()


class _Cfg:
    """Minimal stand-in for AppConfig — only what the checks read."""

    def __init__(self, mode: SystemMode, primary: str = "zerodha", fallback: str = "fyers"):
        self.system = type("s", (), {"mode": mode})()
        self.broker = type("b", (), {"primary": primary, "fallback": fallback})()


class TestCredentialCheckFollowsTheConfiguredBroker:
    def test_live_mode_fails_on_the_primary_brokers_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in ("KITE_API_KEY", "KITE_API_SECRET", "KITE_USER_ID"):
            monkeypatch.delenv(var, raising=False)

        r = doctor.Report()
        with patch("algotrader.common.config.load_config", return_value=_Cfg(SystemMode.LIVE)):
            doctor.check_secrets(r)

        text = " ".join(r.failures)
        assert "KITE_API_KEY" in text, "live mode did not fail on the primary broker's credentials"
        assert "ANGELONE" not in text, (
            "doctor is failing on a broker that is not configured — the original defect"
        )

    def test_a_missing_fallback_credential_does_not_block_live_trading(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fallback is data redundancy, not an order path."""
        for var in ("KITE_API_KEY", "KITE_API_SECRET", "KITE_USER_ID"):
            monkeypatch.setenv(var, "present")
        for var in ("FYERS_APP_ID", "FYERS_SECRET_KEY"):
            monkeypatch.delenv(var, raising=False)

        r = doctor.Report()
        with patch("algotrader.common.config.load_config", return_value=_Cfg(SystemMode.LIVE)):
            doctor.check_secrets(r)

        assert not [f for f in r.failures if "FYERS" in f], (
            "a missing fallback credential should degrade coverage, not stop trading"
        )

    def test_switching_the_configured_broker_switches_what_is_checked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: the check follows config, not a hardcoded list."""
        for profile in PROFILES.values():
            for var in profile.credential_env_vars:
                monkeypatch.delenv(var, raising=False)

        r = doctor.Report()
        cfg = _Cfg(SystemMode.LIVE, primary="angelone", fallback="")
        with patch("algotrader.common.config.load_config", return_value=cfg):
            doctor.check_secrets(r)

        text = " ".join(r.failures)
        assert "ANGELONE_API_KEY" in text
        assert "KITE_API_KEY" not in text

    def test_an_unknown_configured_broker_fails_loudly(self) -> None:
        r = doctor.Report()
        cfg = _Cfg(SystemMode.LIVE, primary="not_a_broker", fallback="")
        with patch("algotrader.common.config.load_config", return_value=cfg):
            doctor.check_secrets(r)
        assert any("not a known profile" in f for f in r.failures)


class TestHolidayCalendarBlocksLiveTrading:
    """BR-20 — verified against the NSE circular before live trading.

    These originally relied on the SHIPPED holiday file being incomplete, which
    made them pass for the wrong reason: they asserted the gate fires, using a
    real defect as the fixture. Closing blocker B3 removed the defect and the
    tests failed — correctly, and usefully. The bad condition is now
    constructed deliberately, so the gate is tested rather than the bug.
    """

    @staticmethod
    def _unverified() -> calendar_mod.HolidayCalendarStatus:
        # Non-empty on purpose: an EMPTY list takes doctor's "NO holiday list
        # loaded" branch, which is a different failure. The condition under
        # test is a list that exists and has not been checked against the
        # circular — the fixed-date-only state B3 described.
        return calendar_mod.HolidayCalendarStatus(
            frozenset({date(2026, 1, 26), date(2026, 12, 25)}),
            verified=False,
            source="test: deliberately unverified",
            path="config/nse_holidays.yaml",
        )

    def test_an_unverified_calendar_fails_in_live_mode(self) -> None:
        r = doctor.Report()
        with (
            patch("algotrader.common.config.load_config", return_value=_Cfg(SystemMode.LIVE)),
            patch.object(
                calendar_mod, "load_holidays_with_status", return_value=self._unverified()
            ),
        ):
            doctor.check_market_calendar(r)

        assert any("INCOMPLETE" in f and "LIVE" in f for f in r.failures), (
            "BR-20 requires an unverified holiday list to block live trading; "
            f"doctor only produced warnings: {r.warnings}"
        )

    def test_the_same_calendar_only_warns_in_paper_mode(self) -> None:
        """Development must stay usable — the gate is for live capital."""
        r = doctor.Report()
        with (
            patch("algotrader.common.config.load_config", return_value=_Cfg(SystemMode.PAPER)),
            patch.object(
                calendar_mod, "load_holidays_with_status", return_value=self._unverified()
            ),
        ):
            doctor.check_market_calendar(r)

        assert not r.failures, f"paper mode should not be blocked: {r.failures}"
        assert any("INCOMPLETE" in w for w in r.warnings)

    def test_the_shipped_calendar_now_passes_the_live_gate(self) -> None:
        """The other half, and the reason the two above needed rewriting: with
        B3 closed the real file must satisfy BR-20 rather than merely warn."""
        r = doctor.Report()
        with patch("algotrader.common.config.load_config", return_value=_Cfg(SystemMode.LIVE)):
            doctor.check_market_calendar(r)
        assert not any("INCOMPLETE" in f for f in r.failures), (
            f"the shipped holiday list should now pass BR-20: {r.failures}"
        )


class TestProfileCredentialsAreCoherent:
    def test_the_configured_brokers_declare_credentials(self) -> None:
        from algotrader.common.config import load_config

        cfg = load_config()
        for key in (cfg.broker.primary, cfg.broker.fallback):
            if key:
                assert get_profile(key).credential_env_vars, (
                    f"{key} declares none, so doctor silently checks nothing for it"
                )
