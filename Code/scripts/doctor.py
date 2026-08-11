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
    except Exception as exc:
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


def check_datastores(r: Report) -> None:
    """Report where the datastore connections actually come from.

    The point of this check is that the override is never silent. `.env` and
    `config/system.yaml` both describe a connection, and if DATABASE_URL is set
    it wins outright — so the question "which database am I actually about to
    talk to?" needs an answer you can read, not one you have to derive.

    This does NOT attempt to connect. Reachability is a separate concern and a
    connection attempt here would make `doctor` fail whenever the containers
    happen to be down, which is not what it is for.
    """
    import os

    section("Datastores")
    try:
        from algotrader.common.config import load_config

        cfg = load_config()
    except Exception:
        # check_config has already reported the reason; do not repeat it.
        r.warn("skipped — config did not load")
        return

    # --- PostgreSQL ---
    if os.environ.get("DATABASE_URL"):
        r.warn(
            "postgres: DATABASE_URL override is ACTIVE",
            "config/system.yaml database: section is being IGNORED",
        )
    elif os.environ.get("POSTGRES_PASSWORD"):
        r.ok("postgres: from system.yaml + POSTGRES_PASSWORD", cfg.database.safe_dsn())
    else:
        r.fail(
            "postgres: no password and no override",
            "set POSTGRES_PASSWORD in .env, or set DATABASE_URL",
        )

    r.ok(
        "postgres pool (configured, not yet applied — E01-S02)",
        f"size={cfg.database.pool_size} overflow={cfg.database.max_overflow} "
        f"timeout={cfg.database.statement_timeout_ms}ms",
    )

    # --- Redis ---
    if os.environ.get("REDIS_URL"):
        r.warn(
            "redis: REDIS_URL override is ACTIVE",
            "config/system.yaml redis: section is being IGNORED",
        )
    else:
        r.ok("redis: from system.yaml", cfg.redis.safe_dsn())

    r.ok("redis stream cap", f"default maxlen={cfg.redis.default_stream_maxlen:,}")

    # --- Migrations ---
    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    revisions = sorted(versions.glob("*.py")) if versions.is_dir() else []
    if not (Path(__file__).resolve().parents[1] / "alembic.ini").exists():
        r.fail("alembic.ini missing", "migrations cannot run")
    elif not revisions:
        r.warn(
            "no migrations written yet",
            "expected during Sprint 1 until E01-S01 lands",
        )
    else:
        r.ok(f"{len(revisions)} migration(s) present", revisions[-1].name)


def check_compliance(r: Report) -> None:
    """SEBI constraints — architectural requirements, not paperwork."""
    section("SEBI compliance")
    try:
        from algotrader.common.config import load_config
        from algotrader.common.enums import SystemMode

        cfg = load_config()
    except Exception:
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
        r.ok(
            f"order rate {rate}/sec",
            f"{profile.display_name} allows {profile.max_orders_per_second}, SEBI 10",
        )
    else:
        r.fail(
            f"order rate {rate}/sec exceeds broker limit",
            f"{profile.display_name} allows only {profile.max_orders_per_second}",
        )

    # Zerodha-style redirect auth cannot complete unattended. That is an
    # operational constraint on autonomy, not a bug — surface it so it is a
    # known trade-off rather than a 07:00 surprise.
    if profile.requires_manual_daily_login:
        r.warn(
            f"{profile.display_name} needs a manual daily login",
            "redirect auth flow - unattended operation needs this solved",
        )
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
        r.ok("single notification recipient", "broadcasting signals can trigger RA obligations")
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

        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as resp:
            actual = resp.read().decode().strip()
    except Exception as exc:
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

    BR-20 requires this to BLOCK live trading, not merely warn about it. An
    incomplete list warns in development, where trading against a phantom
    session costs nothing, and fails in live mode, where it means real orders
    on a day the exchange is shut.
    """
    section("Market calendar")

    from algotrader.common.calendar import DEFAULT_HOLIDAY_FILE, load_holidays_with_status
    from algotrader.common.config import load_config
    from algotrader.common.enums import SystemMode

    try:
        live = load_config().system.mode is SystemMode.LIVE
    except Exception:
        live = False

    path = Path(__file__).parents[1] / DEFAULT_HOLIDAY_FILE
    status = load_holidays_with_status(str(path) if path.exists() else None)

    if status.is_trustworthy:
        r.ok(f"holiday list verified ({status.count} dates)", status.source)
    elif status.count == 0:
        r.fail(
            "NO holiday list loaded",
            "every weekday will be treated as a trading day",
        )
    elif live:
        r.fail(
            f"holiday list INCOMPLETE ({status.count} dates) and mode is LIVE",
            "BR-20: verify against the NSE circular and set "
            "verified_against_nse_circular: true before trading real capital",
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

    Two parameters on ``place_order()`` are checked, and both are regulatory
    rather than cosmetic:

    - ``market_protection`` — MARKET and SL-M orders are rejected without it
      from 1 Apr 2026, so an SDK lacking it cannot square off a position.
      pykiteconnect 5.1.0 omitted it (zerodha/pykiteconnect#225); 5.2.1 has it.
    - ``algo_id`` — SEBI requires every algorithmic order to carry an
      exchange-assigned Algo-ID. Its presence here establishes that the ID is
      **client-supplied per order**, not injected server-side by the broker.
    """
    section("Broker SDK")

    try:
        import kiteconnect
    except ImportError:
        r.warn("kiteconnect not installed", "required before any broker work")
        return

    # NOTE: `kiteconnect.__version__` is a SUBMODULE, not a string, so
    # getattr(kiteconnect, "__version__") returns a module object and prints as
    # "<module 'kiteconnect.__version__' from '...'>". Read the installed
    # distribution metadata instead.
    try:
        from importlib.metadata import version as _dist_version

        version = _dist_version("kiteconnect")
    except Exception:
        version = "unknown"

    try:
        import inspect

        params = inspect.signature(kiteconnect.KiteConnect.place_order).parameters
    except Exception as exc:
        r.warn(f"kiteconnect {version} - could not inspect place_order", str(exc)[:60])
        return

    if "market_protection" in params:
        r.ok(f"kiteconnect {version} supports market_protection")
    else:
        r.fail(
            f"kiteconnect {version} LACKS market_protection",
            "MARKET/SL-M orders will be rejected - upgrade to 5.2.1 or later "
            "(zerodha/pykiteconnect#225)",
        )

    if "algo_id" in params:
        r.ok(f"kiteconnect {version} accepts algo_id per order", "SEBI: client-supplied")
    else:
        r.warn(
            f"kiteconnect {version} has no algo_id parameter",
            "confirm with Zerodha how the Algo-ID is attached (blocker B1)",
        )


def check_strategies(r: Report) -> None:
    section("Strategies")
    try:
        from algotrader.strategy import primitives  # noqa: F401
        from algotrader.strategy.dsl import REGISTRY, load_strategy_yaml

        r.ok(f"{len(REGISTRY.names())} primitives registered")
    except Exception as exc:
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
        except Exception as exc:
            r.fail(f"{path.name} failed to compile", str(exc)[:80])


def check_secrets(r: Report) -> None:
    """Credential presence, for the broker that is actually configured.

    The variable names come from the broker profile, not from a list here. An
    earlier version hardcoded Angel One's three variables while
    ``broker.primary`` was ``zerodha``: it would have hard-failed live mode on
    credentials this deployment never uses, and reported nothing at all about
    the Kite credentials it does. A pre-flight check that validates the wrong
    broker is worse than no check, because it reports green.
    """
    section("Secrets")
    import os

    from algotrader.broker.profiles import get_profile
    from algotrader.common.config import load_config
    from algotrader.common.enums import SystemMode

    live = False
    primary_key = fallback_key = None
    try:
        cfg = load_config()
        live = cfg.system.mode is SystemMode.LIVE
        primary_key = cfg.broker.primary
        fallback_key = cfg.broker.fallback
    except Exception as exc:
        r.warn("could not read broker config", f"{type(exc).__name__}: {exc}")

    for key in ("ANTHROPIC_API_KEY",):
        (r.ok if os.environ.get(key) else (r.fail if live else r.warn))(
            f"{key} {'set' if os.environ.get(key) else 'not set'}"
        )

    def _check(broker_key: str | None, *, role: str, blocks_live: bool) -> None:
        if not broker_key:
            return
        try:
            profile = get_profile(broker_key)
        except ValueError as exc:
            r.fail(f"{role} broker {broker_key!r} is not a known profile", str(exc))
            return
        if not profile.credential_env_vars:
            r.warn(
                f"{profile.display_name} declares no credential variables",
                f"add credential_env_vars to the {broker_key} profile before using it",
            )
            return
        for key in profile.credential_env_vars:
            if os.environ.get(key):
                r.ok(f"{key} set", f"{role}: {profile.display_name}")
            elif live and blocks_live:
                r.fail(f"{key} not set", f"required for live trading ({profile.display_name})")
            else:
                r.warn(f"{key} not set", f"{role}: {profile.display_name}")

    _check(primary_key, role="primary", blocks_live=True)
    # The fallback is data redundancy, not an order path, so a missing
    # credential there degrades coverage rather than stopping trading.
    if fallback_key and fallback_key != primary_key:
        _check(fallback_key, role="fallback", blocks_live=False)

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
    check_datastores(r)
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
