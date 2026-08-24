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

import importlib.metadata as md

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
        """`market_protection` and `algo_id` are compliance requirements, not
        conveniences. If the replacement ever changed the SDK's surface, an
        order would be rejected at the exchange rather than here."""
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
