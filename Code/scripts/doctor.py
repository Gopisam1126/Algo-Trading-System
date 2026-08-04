#!/usr/bin/env python
"""Pre-flight check.

Run this before every trading day and after any infrastructure change.  It
verifies the things that are cheap to check and expensive to get wrong —
particularly the SEBI compliance constraints, which are architectural
requirements rather than paperwork.

    python scripts/doctor.py

Exit code 0 = ready, 1 = problems found.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

# Windows consoles default to cp1252; force UTF-8 so output never mangles.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
OK, FAIL, WARN = f"{GREEN}[ OK ]{RESET}", f"{RED}[FAIL]{RESET}", f"{YELLOW}[WARN]{RESET}"


@dataclass
class Report:
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ok(self, msg: str, detail: str = "") -> None:
        print(f"  {OK} {msg}" + (f" {DIM}{detail}{RESET}" if detail else ""))

    def fail(self, msg: str, detail: str = "") -> None:
        print(f"  {FAIL} {msg}" + (f" {DIM}{detail}{RESET}" if detail else ""))
        self.failures.append(msg)

    def warn(self, msg: str, detail: str = "") -> None:
        print(f"  {WARN} {msg}" + (f" {DIM}{detail}{RESET}" if detail else ""))
        self.warnings.append(msg)


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def check_python(r: Report) -> None:
    section("Environment")
    v = sys.version_info
    if v >= (3, 12):
        r.ok(f"Python {v.major}.{v.minor}.{v.micro}")
    elif v >= (3, 11):
        r.warn(f"Python {v.major}.{v.minor}.{v.micro}", "3.12+ recommended")
    else:
        r.fail(f"Python {v.major}.{v.minor} is too old", "3.11 minimum")


def check_dependencies(r: Report) -> None:
    required = {
        "pydantic": "core validation",
        "yaml": "config loading",
        "redis": "message fabric",
        "sqlalchemy": "persistence",
        "structlog": "logging + redaction",
        "anthropic": "AI layer",
        "fastapi": "dashboard",
    }
    optional = {"talib": "indicator hot path", "uvloop": "faster event loop"}

    for mod, why in required.items():
        try:
            __import__(mod)
            r.ok(f"{mod}", why)
        except ImportError:
            r.fail(f"{mod} not installed", f"needed for {why} - run: make install")

    for mod, why in optional.items():
        try:
            __import__(mod)
            r.ok(f"{mod}", why)
        except ImportError:
            r.warn(f"{mod} not installed", why)


def check_config(r: Report) -> None:
    section("Configuration")
    try:
        from algotrader.common.config import load_config

        cfg = load_config()
    except FileNotFoundError as exc:
        r.fail("config not found", str(exc))
        return
    except Exception as exc:  # noqa: BLE001 — surface any validation error verbatim
        r.fail("config failed validation", str(exc).split("\n")[0])
        return

    r.ok("config valid", f"hash={cfg.config_hash()}")
    r.ok(f"mode = {cfg.system.mode.value}")
    r.ok(f"autonomy = {cfg.autonomy.level.value}")

    print(f"\n  {DIM}Derived risk figures:{RESET}")
    print(f"    capital            Rs {cfg.risk.capital:>12,.0f}")
    print(f"    slots              {cfg.risk.position_slots:>15}")
    print(f"    capital per slot   Rs {cfg.risk.capital_per_slot:>12,.0f}")
    print(f"    risk per trade     Rs {cfg.risk.risk_per_trade_rupees:>12,.0f}")
    print(f"    daily loss limit   Rs {cfg.risk.daily_loss_limit_rupees:>12,.0f}")


def check_compliance(r: Report) -> None:
    """SEBI constraints — architectural requirements, not paperwork."""
    section("SEBI compliance")
    try:
        from algotrader.common.config import load_config
        from algotrader.common.enums import SystemMode

        cfg = load_config()
    except Exception:  # noqa: BLE001
        r.fail("cannot check compliance", "config did not load")
        return

    live = cfg.system.mode is SystemMode.LIVE

    # The binding constraint is min(SEBI cap, broker API limit).
    try:
        from algotrader.broker.profiles import get_profile

        profile = get_profile(cfg.broker.primary)
    except ValueError as exc:
        r.fail("unknown broker in config", str(exc)[:80])
        return

    rate = cfg.execution.max_orders_per_second
    if rate <= profile.max_orders_per_second:
        r.ok(f"order rate {rate}/sec",
             f"{profile.display_name} allows {profile.max_orders_per_second}, SEBI 10")
    else:
        r.fail(f"order rate {rate}/sec exceeds broker limit",
               f"{profile.display_name} allows only {profile.max_orders_per_second}")

    # Zerodha-style redirect auth cannot complete unattended. That is an
    # operational constraint on autonomy, not a bug — surface it so it is a
    # known trade-off rather than a 07:00 surprise.
    if profile.requires_manual_daily_login:
        r.warn(f"{profile.display_name} needs a manual daily login",
               "redirect auth flow - unattended operation needs this solved")
    else:
        r.ok(f"{profile.display_name} supports programmatic daily login")

    india = {"ap-south-1", "ap-south-2", "centralindia", "southindia", "asia-south1"}
    if cfg.system.deployment_region.lower() in india:
        r.ok(f"region {cfg.system.deployment_region}", "India - required by SEBI")
    else:
        r.fail(f"region {cfg.system.deployment_region} is not in India")

    if cfg.system.static_ip:
        r.ok("static IP configured", cfg.system.static_ip)
    elif live:
        r.fail("static IP required for live mode")
    else:
        r.warn("static IP not set", "required before live trading")

    if cfg.broker.algo_id:
        r.ok("Algo-ID configured")
    elif live:
        r.fail("Algo-ID required for live mode")
    else:
        r.warn("Algo-ID not set", "required before live trading")

    if len(cfg.notifications.recipients) <= 1:
        r.ok("single notification recipient",
             "broadcasting signals can trigger RA obligations")
    else:
        r.fail("multiple notification recipients configured")


def check_egress_ip(r: Report) -> None:
    """The IP the broker sees must match what is whitelisted."""
    section("Network")
    import os

    expected = os.environ.get("EXPECTED_EGRESS_IP", "").strip()
    if not expected:
        r.warn("EXPECTED_EGRESS_IP not set", "cannot verify broker whitelist match")
        return
    try:
        import urllib.request

        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as resp:  # noqa: S310
            actual = resp.read().decode().strip()
    except Exception as exc:  # noqa: BLE001
        r.warn("could not determine egress IP", str(exc)[:60])
        return

    if actual == expected:
        r.ok("egress IP matches whitelist", actual)
    else:
        r.fail("egress IP MISMATCH", f"expected {expected}, got {actual}")


def check_market_calendar(r: Report) -> None:
    """An incomplete holiday list is a SILENT failure.

    The system would treat a market holiday as a normal trading day, run the
    pre-market pipeline against data that never arrives, and potentially
    compute indicators across a phantom session. Nothing errors — it just
    quietly does the wrong thing. Hence an explicit check.
    """
    section("Market calendar")

    from algotrader.common.calendar import DEFAULT_HOLIDAY_FILE, load_holidays_with_status

    path = Path(__file__).parents[1] / DEFAULT_HOLIDAY_FILE
    status = load_holidays_with_status(str(path) if path.exists() else None)

    if status.is_trustworthy:
        r.ok(f"holiday list verified ({status.count} dates)", status.source)
    elif status.count == 0:
        r.fail(
            "NO holiday list loaded",
            "every weekday will be treated as a trading day",
        )
    else:
        r.warn(
            f"holiday list INCOMPLETE ({status.count} dates)",
            "fixed-date only; lunar festivals missing - populate from the NSE circular",
        )

    # Sanity: a known fixed holiday must actually be excluded.
    from datetime import date as _date

    from algotrader.common.calendar import MarketCalendar

    cal = MarketCalendar(status.dates)
    republic_day = _date(2026, 1, 26)
    if status.count:
        if cal.is_trading_day(republic_day):
            r.fail("Republic Day treated as a trading day", "holiday list is not applied")
        else:
            r.ok("holiday exclusion works", "26 Jan 2026 correctly excluded")


def check_broker_sdk(r: Report) -> None:
    """Verify the installed Kite SDK can actually place compliant orders.

    pykiteconnect 5.1.0 on PyPI omits `market_protection` from place_order().
    MARKET and SL-M orders are rejected without it from 1 Apr 2026, so a plain
    `pip install kiteconnect` yields an SDK that cannot square off a position.
    """
    section("Broker SDK")

    try:
        import kiteconnect
    except ImportError:
        r.warn("kiteconnect not installed", "required before any broker work")
        return

    version = getattr(kiteconnect, "__version__", "unknown")

    try:
        import inspect

        params = inspect.signature(kiteconnect.KiteConnect.place_order).parameters
    except Exception as exc:  # noqa: BLE001
        r.warn(f"kiteconnect {version} - could not inspect place_order", str(exc)[:60])
        return

    if "market_protection" in params:
        r.ok(f"kiteconnect {version} supports market_protection")
    else:
        r.fail(
            f"kiteconnect {version} LACKS market_protection",
            "MARKET/SL-M orders will be rejected - install from git main "
            "(zerodha/pykiteconnect#225)",
        )


def check_strategies(r: Report) -> None:
    section("Strategies")
    try:
        from algotrader.strategy import primitives  # noqa: F401
        from algotrader.strategy.dsl import REGISTRY, load_strategy_yaml

        r.ok(f"{len(REGISTRY.names())} primitives registered")
    except Exception as exc:  # noqa: BLE001
        r.fail("primitive registry failed to load", str(exc)[:80])
        return

    strategy_dir = Path(__file__).parents[1] / "config" / "strategies"
    files = sorted(strategy_dir.glob("*.yaml")) if strategy_dir.exists() else []
    if not files:
        r.warn("no strategy files found", str(strategy_dir))
        return

    from algotrader.strategy.dsl import compile_strategy

    for path in files:
        try:
            doc = load_strategy_yaml(path.read_text(encoding="utf-8"))
            compile_strategy(doc)
            r.ok(f"{path.name}", f"{doc.id} v{doc.version}")
        except Exception as exc:  # noqa: BLE001
            r.fail(f"{path.name} failed to compile", str(exc)[:80])


def check_secrets(r: Report) -> None:
    section("Secrets")
    import os

    from algotrader.common.config import load_config
    from algotrader.common.enums import SystemMode

    try:
        live = load_config().system.mode is SystemMode.LIVE
    except Exception:  # noqa: BLE001
        live = False

    required = ["ANTHROPIC_API_KEY"]
    broker = ["ANGELONE_API_KEY", "ANGELONE_CLIENT_ID", "ANGELONE_TOTP_SECRET"]

    for key in required:
        (r.ok if os.environ.get(key) else (r.fail if live else r.warn))(
            f"{key} {'set' if os.environ.get(key) else 'not set'}"
        )
    for key in broker:
        if os.environ.get(key):
            r.ok(f"{key} set")
        elif live:
            r.fail(f"{key} not set", "required for live trading")
        else:
            r.warn(f"{key} not set", "required before live trading")

    if Path(".env").exists():
        import stat

        mode = Path(".env").stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            r.warn(".env is group/world readable", "chmod 600 .env")
        else:
            r.ok(".env permissions look sane")


def main() -> int:
    print("=" * 62)
    print("  AI Algo Trading - pre-flight check")
    print("=" * 62)

    r = Report()
    check_python(r)
    check_dependencies(r)
    check_config(r)
    check_compliance(r)
    check_secrets(r)
    check_broker_sdk(r)
    check_market_calendar(r)
    check_strategies(r)
    check_egress_ip(r)

    print("\n" + "=" * 62)
    if r.failures:
        print(f"  {RED}{len(r.failures)} failure(s){RESET}, {len(r.warnings)} warning(s)")
        for f in r.failures:
            print(f"    - {f}")
        print("=" * 62)
        return 1

    print(f"  {GREEN}All checks passed{RESET}, {len(r.warnings)} warning(s)")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
