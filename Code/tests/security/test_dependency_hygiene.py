"""The installed dependency graph, checked rather than assumed.

**Why this file exists.** Blocker B7 — `kiteconnect` ships a hard
`autobahn[twisted]==19.11.2` pin, and 19.11.2 carries CVE-2020-35678. The fix
cannot be a version floor in `pyproject.toml`: `==` is not satisfiable
alongside `>=20.12.3`, so pip fails outright with ResolutionImpossible. That
mistake broke every CI job for two commits, and it broke them *only* in CI —
locally the package had been force-installed over an already-resolved
environment, which pip permits with a warning.

So the fix is a post-install replacement:

    pip install -c constraints.txt -e ".[dev]"
    pip install --no-deps --upgrade "autobahn==26.7.1"

which is applied in `ci.yml`, `ops/Dockerfile` and the `Makefile`. Four places,
and a control that has to be remembered in four places will eventually be
forgotten in one.

**This test is the enforcement.** Documentation cannot fail a build; a test
can. Any environment that runs the suite without the override fails here,
loudly, naming the command to run — which turns "someone forgot" from a silent
CVE into a red check.
"""

from __future__ import annotations

import ast
import importlib.metadata as md
import subprocess
import sys
from pathlib import Path

import pytest

#: First autobahn release carrying the fix for CVE-2020-35678 (redirect header
#: injection in the WebSocket client).
FIRST_PATCHED_AUTOBAHN = (20, 12, 3)

#: What the install steps pin to. Kept explicit so a drift between this file
#: and the install commands is visible rather than inferred.
EXPECTED_AUTOBAHN = "26.7.1"


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in text.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


class TestAutobahnIsNotTheVulnerableVersion:
    def test_the_installed_autobahn_is_patched(self) -> None:
        try:
            installed = md.version("autobahn")
        except md.PackageNotFoundError:  # pragma: no cover - autobahn is a transitive dep
            pytest.skip("autobahn is not installed in this environment")

        assert _version_tuple(installed) >= FIRST_PATCHED_AUTOBAHN, (
            f"autobahn {installed} is installed and carries CVE-2020-35678.\n"
            f"kiteconnect pins it at 19.11.2, so resolution alone cannot fix "
            f"this — the install must REPLACE it afterwards:\n\n"
            f'    pip install --no-deps --upgrade "autobahn=={EXPECTED_AUTOBAHN}"\n\n'
            f"See pyproject.toml, .github/workflows/ci.yml and ops/Dockerfile."
        )

    def test_kiteconnect_still_imports_under_the_replacement(self) -> None:
        """The whole reason the replacement is safe.

        `kiteconnect`'s pin is DECLARATIVE — it is not a runtime requirement.
        This asserts that, rather than trusting it: if a future kiteconnect
        genuinely needed 19.11.2's API, this is where it would surface.
        """
        pytest.importorskip("kiteconnect")
        from kiteconnect import KiteConnect, exceptions

        client = KiteConnect(api_key="probe")
        assert client.login_url().startswith("https://")
        assert issubclass(exceptions.TokenException, Exception)

    def test_the_order_parameters_we_depend_on_survive(self) -> None:
        """`market_protection` is a compliance requirement — Zerodha rejects
        an unprotected MARKET order outright. `algo_id` is optional and only
        needed above the registration threshold, but its presence is still
        worth pinning: it is the parameter that would carry a registered
        Algo-ID if this system ever crossed into that regime. Either way, a
        replacement SDK that changed this surface would fail at the exchange
        rather than here."""
        pytest.importorskip("kiteconnect")
        import inspect

        from kiteconnect import KiteConnect

        params = inspect.signature(KiteConnect(api_key="probe").place_order).parameters
        assert "market_protection" in params
        assert "algo_id" in params


class TestTheInstallSitesAgree:
    """A version that drifts between install sites produces an environment
    nobody described. These are cheap string checks; the point is that all four
    sites move together."""

    @staticmethod
    def _read(*parts: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[2].joinpath(*parts)).read_text(encoding="utf-8")

    def test_ci_applies_the_override_in_every_install_job(self) -> None:
        workflow = self._read("..", ".github", "workflows", "ci.yml")
        occurrences = workflow.count(f"autobahn=={EXPECTED_AUTOBAHN}")
        installs = workflow.count('pip install -c constraints.txt -e ".[dev]"')
        assert occurrences == installs, (
            f"{installs} install step(s) but {occurrences} override(s) — "
            f"a job installing without the override ships the CVE."
        )

    def test_the_dockerfile_applies_it_too(self) -> None:
        assert f"autobahn=={EXPECTED_AUTOBAHN}" in self._read("ops", "Dockerfile")

    def test_constraints_records_what_resolution_produces(self) -> None:
        """Not 26.7.1: constraining autobahn to the replacement makes the
        resolution itself impossible, which is the bug this whole file
        documents."""
        constraints = self._read("constraints.txt")
        assert "autobahn==19.11.2" in constraints

    def test_pyproject_does_not_declare_an_unsatisfiable_floor(self) -> None:
        """The regression that broke CI. A floor here cannot coexist with
        kiteconnect's `==` pin, and pip fails rather than warning."""
        pyproject = self._read("pyproject.toml")
        active = [
            line
            for line in pyproject.splitlines()
            if "autobahn" in line and not line.strip().startswith("#")
        ]
        assert not active, f"autobahn declared as a dependency again: {active}"


class TestTheBuildEnvironmentIsAudited:
    """The finding that broke CI was not in anything `constraints.txt`
    describes.

    `actions/setup-python` ships setuptools in the 65-68 range, which carries
    eight advisories, and `pip install --upgrade pip` leaves it exactly where
    it was. The project's own pinned set audited clean the whole time — the
    vulnerable package was the BUILD backend, which nothing pins and nothing
    was looking at.

    It was invisible locally for a mundane reason: a recently-created virtualenv
    already has a current setuptools, so the same command passes on a laptop and
    fails on a runner. That is the shape of environment defect these tests
    exist to catch.
    """

    #: Upgraded rather than pinned. These are the build environment, not the
    #: application's dependency graph — the correct version is "current".
    BUILD_BACKEND = ("pip", "setuptools", "wheel")

    @staticmethod
    def _read(*parts: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[2].joinpath(*parts)).read_text(encoding="utf-8")

    def test_every_ci_install_upgrades_the_build_backend(self) -> None:
        workflow = self._read("..", ".github", "workflows", "ci.yml")
        upgrades = workflow.count("pip install --upgrade pip setuptools wheel")
        installs = workflow.count('pip install -c constraints.txt -e ".[dev]"')
        assert upgrades == installs, (
            f"{installs} install step(s) but {upgrades} build-backend "
            f"upgrade(s). A job that skips it audits a setuptools with known "
            f"advisories and fails for a reason unrelated to this project."
        )

    def test_the_dockerfile_upgrades_it_too(self) -> None:
        dockerfile = self._read("ops", "Dockerfile")
        assert "--upgrade pip setuptools wheel" in dockerfile

    def test_the_locally_installed_build_backend_is_not_vulnerable(self) -> None:
        """A weak local check — it cannot see the runner's versions — but it
        catches a developer whose environment has drifted far enough to
        reproduce the CI failure."""
        import importlib.metadata as md

        stale = []
        for name in self.BUILD_BACKEND:
            try:
                version = md.version(name)
            except md.PackageNotFoundError:
                continue
            if name == "setuptools" and _version_tuple(version) < (78, 1, 1):
                stale.append(f"{name}=={version}")
        assert not stale, (
            f"build backend carries known advisories: {stale}. "
            f"Run: pip install --upgrade pip setuptools wheel"
        )


class TestLoadingAConfigDoesNotDragInTheBrokerStack:
    """AUDIT-004, found by the end-to-end audit's import-graph pass.

    ``AppConfig`` has one import that points *upward*: a deferred
    ``from algotrader.broker.profiles import get_profile`` inside
    ``_order_rate_within_broker_limit``. Deferring it is correct and
    deliberate — it breaks a cycle, and ``broker/profiles.py`` is pure data.

    What makes it safe is a second fact that is nowhere written down:
    ``broker/__init__.py`` is **empty**. Executing the package to reach
    ``profiles`` therefore costs nothing. Add one convenience re-export there
    — the most natural edit in the world, and one no reviewer would question —
    and every process that validates a config starts importing ``kiteconnect``,
    which imports ``.ticker`` unconditionally, which loads autobahn and
    Twisted. That is recorded in CLAUDE.md as an already-made mistake:
    *not using a package is not the same as not having it.*

    The cost is not only startup time. It pulls the one dependency in this
    project with a CVE history into processes that never touch a broker, and
    it does so silently: nothing fails, nothing logs, and the existing
    dependency tests here check autobahn's *version*, never whether it was
    loaded at all.

    So the emptiness is load-bearing, and this asserts it as behaviour rather
    than trusting a file to stay empty.
    """

    #: Third-party packages that must not be reachable from config validation.
    FORBIDDEN = frozenset({"twisted", "autobahn", "kiteconnect"})

    def test_validating_a_config_loads_no_broker_transport(self) -> None:
        """Run in a subprocess: the parent's ``sys.modules`` is polluted by
        every other test in the suite, so an in-process check would pass or
        fail on import order rather than on the thing being asserted."""
        program = (
            "import sys\n"
            "from algotrader.common.config import AppConfig\n"
            "AppConfig()\n"
            "roots = {m.split('.')[0] for m in sys.modules}\n"
            "print(','.join(sorted(roots & {'twisted', 'autobahn', 'kiteconnect'})))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
        assert result.returncode == 0, result.stderr
        loaded = [name for name in result.stdout.strip().split(",") if name]
        assert not loaded, (
            f"validating an AppConfig imported {loaded}. Something in the chain "
            f"algotrader.broker -> algotrader.broker.profiles now pulls in the "
            f"broker transport. Keep broker/__init__.py free of re-exports."
        )

    def test_the_deferred_import_is_still_the_only_upward_one(self) -> None:
        """The control on the assertion above: it is only meaningful while the
        import is deferred. A module-level ``import`` of anything under
        ``algotrader.broker`` in ``common/`` would make config validation eager
        regardless of what this test's subprocess finds today."""
        common = Path(__file__).resolve().parents[2] / "src" / "algotrader" / "common"
        offenders: list[str] = []
        for path in common.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if not node.module.startswith("algotrader.broker"):
                    continue
                # col_offset 0 == module level; anything indented is deferred.
                if node.col_offset == 0:
                    offenders.append(f"{path.name}:{node.lineno} -> {node.module}")
        assert not offenders, (
            f"common/ imports broker at module level: {offenders}. "
            f"common is the lowest layer; the one permitted upward reference "
            f"(config's broker-rate validator) is deferred on purpose."
        )
