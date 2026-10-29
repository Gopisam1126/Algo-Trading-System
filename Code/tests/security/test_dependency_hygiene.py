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
    pip install --no-deps --upgrade "autobahn==26.10.2"

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
import re
import subprocess
import sys
from pathlib import Path

import pytest

#: First autobahn release carrying the fix for CVE-2020-35678 (redirect header
#: injection in the WebSocket client).
FIRST_PATCHED_AUTOBAHN = (20, 12, 3)

#: First release fixing CVE-2026-116579 / GHSA-pjh6-c8mg-3x7m (published 28 Oct
#: 2026): permessage-brotli inflated a whole frame before the message-size
#: check, so one 421-byte frame cost a SERVER ~545 MiB. Unreachable here - this
#: system runs no autobahn server and enables no compression extension - and
#: fixed anyway, because a fix existed and acceptance is for what cannot be.
BROTLI_FIX_AUTOBAHN = (26, 10, 1)

#: What the install steps pin to. Kept explicit so a drift between this file
#: and the install commands is visible rather than inferred.
EXPECTED_AUTOBAHN = "26.10.2"


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

    def test_the_installed_autobahn_carries_the_brotli_fix(self) -> None:
        """26.7.1 cleared CVE-2020-35678 and carries CVE-2026-116579; a
        rollback to it must fail here, not only in the audit job."""
        try:
            installed = md.version("autobahn")
        except md.PackageNotFoundError:  # pragma: no cover - autobahn is a transitive dep
            pytest.skip("autobahn is not installed in this environment")
        assert _version_tuple(installed) >= BROTLI_FIX_AUTOBAHN, (
            f"autobahn {installed} carries CVE-2026-116579; install "
            f'"autobahn=={EXPECTED_AUTOBAHN}" (see pyproject.toml)'
        )
        assert _version_tuple(EXPECTED_AUTOBAHN) >= BROTLI_FIX_AUTOBAHN

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
        """Not 26.10.2: constraining autobahn to the replacement makes the
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

    def test_make_install_builds_the_environment_ci_builds(self) -> None:
        """The developer install site, which these tests did not cover. It
        installed unconstrained and upgraded only pip, so a local venv
        resolved its own versions - 17 differed from constraints.txt on 27 Oct
        2026 and local audits checked packages nothing ships (AUDIT-022)."""
        makefile = self._read("Makefile")
        install = makefile.split("\ninstall:", 1)[1].split("\n.PHONY", 1)[0]
        assert "--upgrade pip setuptools wheel" in install
        assert '-c constraints.txt -e ".[dev]"' in install
        assert "patch-deps" in install
        assert f"autobahn=={EXPECTED_AUTOBAHN}" in makefile

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
            if name == "setuptools" and _version_tuple(version) < (84, 1, 0):
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


#: Advisories the dependency audit is told to ignore. Each maps to the package,
#: the exact version it was assessed against, and the SUBPACKAGE the vulnerable
#: code lives in. An ignore is a CLAIM — "this vulnerability is present but
#: unreachable" — and the class below makes each claim executable instead of
#: leaving it in a comment that outlives its truth.
#:
#: Every one is in a Twisted subsystem this process never loads - six servers
#: and the DNS client. Assessed 15 Oct 2026 (the first) and 27 Oct 2026 (the
#: rest; CVE-2026-83813 was published between the local audit and the CI run
#: that morning). On 27 Oct they were fixed only in 26.10.0rc1/rc2 — a
#: pre-release, not pinned into a system that moves money when the vulnerable
#: code cannot run anyway.
ACCEPTED_ADVISORIES: dict[str, tuple[str, str, str]] = {
    # IMAP4 server: LIST/LSUB wildcard compiled as a regex (ReDoS).
    "CVE-2026-106454": ("twisted", "26.4.0", "twisted.mail"),
    # IMAP4 server: negative literal size loops parseNestedParens forever.
    "CVE-2026-115774": ("twisted", "26.4.0", "twisted.mail"),
    # XML element stream (XMPP): an unclosed container grows without bound.
    "CVE-2026-115775": ("twisted", "26.4.0", "twisted.words"),
    # IRC server: per-connection line buffer has no maximum.
    "CVE-2026-115776": ("twisted", "26.4.0", "twisted.words"),
    # Telnet transport: subnegotiation buffer has no maximum.
    "CVE-2026-115778": ("twisted", "26.4.0", "twisted.conch"),
    # HTTP server: bare CR/LF in a header value hides a header (smuggling).
    "CVE-2026-115877": ("twisted", "26.4.0", "twisted.web"),
    # DNS client: a response is accepted on the 16-bit transaction id alone
    # (cache poisoning). Twisted's reactor resolves through the system
    # resolver by default; twisted.names is used only when installed
    # explicitly, and nothing here does - or runs the reactor at all.
    "CVE-2026-83813": ("twisted", "26.4.0", "twisted.names"),
}

#: The module each subsystem's advisory is actually in — imported by the
#: control to prove the probe would see it.
VULNERABLE_MODULES = (
    "twisted.mail.imap4",
    "twisted.words.xish.domish",
    "twisted.words.protocols.irc",
    "twisted.conch.telnet",
    "twisted.web.http",
    "twisted.names.dns",
)


def _twisted_modules_loaded_by_the_application(prelude: str = "") -> list[str]:
    """Import kiteconnect and EVERY algotrader module in a clean interpreter,
    and report which Twisted modules ended up loaded.

    A subprocess for the same reason as the config check above: the parent's
    ``sys.modules`` reflects whatever the rest of the suite imported. Walking
    every package rather than naming entry points means a new module that
    reaches for a vulnerable subsystem is caught without anyone remembering to
    list it here.
    """
    program = (
        "import importlib, pkgutil, sys\n"
        f"{prelude}\n"
        "import kiteconnect\n"
        "import algotrader\n"
        "for info in pkgutil.walk_packages(algotrader.__path__, 'algotrader.'):\n"
        "    importlib.import_module(info.name)\n"
        "print(','.join(sorted(n for n in sys.modules if n.split('.')[0] == 'twisted')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert result.returncode == 0, result.stderr
    return [name for name in result.stdout.strip().split(",") if name]


def _within(module: str, package: str) -> bool:
    prefix = package.split(".")
    return module.split(".")[: len(prefix)] == prefix


def _twisted_imports_in(source: str) -> list[str]:
    """Every import of twisted or autobahn in a piece of source."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            continue
        found.extend(n for n in names if n.split(".")[0] in {"twisted", "autobahn"})
    return found


def _audit_invocations(text: str) -> list[str]:
    """Every pip-audit command in a shell-ish file, with backslash
    continuations joined so a command split over lines is read whole.
    Comment lines and YAML keys such as ``name: pip-audit`` are not commands."""
    joined = re.sub(r"[ \t]*\\\r?\n\s*", " ", text)
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip().startswith("pip-audit ") or line.strip() == "pip-audit"
    ]


class TestTheAcceptedTwistedAdvisories:
    """Six advisories against Twisted 26.4.0, all in its SERVER subsystems:
    the IMAP4 server (two), the XMPP XML stream, the IRC server, the telnet
    transport and the HTTP server. See ACCEPTED_ADVISORIES for each.

    Twisted is here only because kiteconnect pins ``autobahn[twisted]`` and
    imports ``.ticker`` unconditionally (see the B7 classes above). It cannot
    be removed — ``import kiteconnect`` fails without it — and on the day each
    was assessed no stable release fixed it.

    What makes them safe is that the vulnerable code cannot run here. Every one
    needs a Twisted server of that protocol accepting connections; this system
    runs none, never runs the Twisted reactor, and never even imports
    ``twisted.mail``, ``twisted.words``, ``twisted.conch`` or ``twisted.web``.
    The audit therefore ignores exactly these IDs — never the package, so any
    OTHER Twisted advisory still fails the gate — and each part of that
    argument is asserted below, because the lesson of QA-SEC-24 is that "we do
    not use it" was once recorded as fact and was false.
    """

    @staticmethod
    def _read(*parts: str) -> str:
        return (Path(__file__).resolve().parents[2].joinpath(*parts)).read_text(encoding="utf-8")

    def _audit_commands(self) -> dict[str, list[str]]:
        return {
            "ci.yml": _audit_invocations(self._read("..", ".github", "workflows", "ci.yml")),
            "Makefile": _audit_invocations(self._read("Makefile")),
        }

    def test_every_audit_ignores_exactly_the_accepted_set(self) -> None:
        """Not a superset: an ignore with no assessment behind it is a hole,
        and an ignore left behind after the reason expired is a hole that
        nobody is looking at."""
        commands = self._audit_commands()
        assert all(commands.values()), f"an audit site has no pip-audit command: {commands}"
        for site, lines in commands.items():
            for line in lines:
                ignored = set(re.findall(r"--ignore-vuln\s+(\S+)", line))
                assert ignored == set(ACCEPTED_ADVISORIES), (
                    f"{site}: `{line}` ignores {sorted(ignored)}, but the "
                    f"assessed set is {sorted(ACCEPTED_ADVISORIES)}."
                )

    def test_every_audit_is_skip_editable_and_not_strict(self) -> None:
        for site, lines in self._audit_commands().items():
            for line in lines:
                assert "--skip-editable" in line, f"{site}: `{line}`"
                assert "--strict" not in line, f"{site}: `{line}`"

    def test_a_continued_command_is_read_whole(self) -> None:
        """Control for the reader above: a command split with backslashes is
        one command, and comments and YAML keys are not commands."""
        text = (
            "# pip-audit is a gate\n"
            "  name: pip-audit\n"
            "  pip-audit --skip-editable \\\n"
            "    --ignore-vuln A \\\n"
            "    --ignore-vuln B | tee out\n"
        )
        assert _audit_invocations(text) == [
            "pip-audit --skip-editable --ignore-vuln A --ignore-vuln B | tee out"
        ]

    def test_the_assessment_is_of_the_installed_version(self) -> None:
        """The re-assessment trigger. An assessment holds for one version; when
        Twisted moves, someone must decide again — a fixed release means the
        ignore comes OUT, an unfixed one means these checks are re-read."""
        for advisory, (package, assessed, _) in ACCEPTED_ADVISORIES.items():
            installed = md.version(package)
            assert installed == assessed, (
                f"{package} is {installed}, but {advisory} was assessed against "
                f"{assessed}. Re-assess: if {installed} fixes it, remove the "
                f"--ignore-vuln from ci.yml and the Makefile and drop the entry "
                f"from ACCEPTED_ADVISORIES."
            )

    def test_the_constraints_pin_the_assessed_version(self) -> None:
        constraints = self._read("constraints.txt").lower()
        for package, assessed, _ in ACCEPTED_ADVISORIES.values():
            assert f"{package}=={assessed}" in constraints

    def test_no_vulnerable_subsystem_is_ever_loaded(self) -> None:
        loaded = _twisted_modules_loaded_by_the_application()
        assert loaded, (
            "no Twisted module loaded at all — kiteconnect no longer drags it "
            "in, so this probe is not measuring anything. Re-assess whether "
            "Twisted is still a dependency."
        )
        for advisory, (_, _, subsystem) in ACCEPTED_ADVISORIES.items():
            reached = [n for n in loaded if _within(n, subsystem)]
            assert not reached, (
                f"{reached} is loaded by the application. {advisory} lives in "
                f"{subsystem} and is ignored by the audit ONLY because that "
                f"subsystem was unreachable."
            )

    def test_the_probe_sees_every_vulnerable_module_when_it_is_loaded(self) -> None:
        """The control. Without it, a probe that reported nothing — wrong
        prefix, a walk that silently skipped everything — would pass the test
        above forever. One subprocess imports all five modules."""
        loaded = _twisted_modules_loaded_by_the_application(
            "\n".join(f"import {m}" for m in VULNERABLE_MODULES)
        )
        for module in VULNERABLE_MODULES:
            assert module in loaded, module

    def test_every_accepted_subsystem_has_a_module_the_control_loads(self) -> None:
        """Keeps the control honest as the list grows: an advisory whose
        subsystem the control never imports is an unprobed claim."""
        for advisory, (_, _, subsystem) in ACCEPTED_ADVISORIES.items():
            assert any(_within(m, subsystem) for m in VULNERABLE_MODULES), advisory

    def test_no_application_code_imports_twisted(self) -> None:
        """The stronger claim behind "no server": nothing here touches Twisted
        or autobahn directly. The live feed is ``websockets`` against the
        documented binary protocol (ingest/kite_protocol.py)."""
        src = Path(__file__).resolve().parents[2] / "src"
        offenders = [
            f"{path.relative_to(src)} -> {name}"
            for path in src.rglob("*.py")
            for name in _twisted_imports_in(path.read_text(encoding="utf-8"))
        ]
        assert not offenders, f"application code imports Twisted/autobahn: {offenders}"

    def test_the_source_scan_finds_an_import_when_there_is_one(self) -> None:
        """Control for the scan above."""
        assert _twisted_imports_in("from twisted.mail import imap4\n") == ["twisted.mail"]
        assert _twisted_imports_in("import autobahn.twisted\n") == ["autobahn.twisted"]
        assert _twisted_imports_in("import twistedish\n") == []
