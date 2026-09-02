"""The suite must not silently disable its own coverage.

Found during QA-2 for E14-S02. The Docker gate in ``tests/conftest.py`` read

    if "integration" in item.keywords:

which looks equivalent to checking the marker and is not: ``item.keywords``
also contains every ancestor node name, so the **directory**
``tests/integration/`` matched it. Every test underneath was gated on Docker
whether or not it touched a container.

What that disabled matters more than the mechanism. ``test_tick_to_trigger.py``
uses no container fixture at all — it is ticks through cleaning, bars,
indicators, the strategy runtime and now the risk engine, all pure computation.
It is also the only system-level test in the repository, added precisely
because component tests cannot see what it sees, and it found a HIGH-severity
fail-open on its first run. On any machine without Docker Desktop it reported
as skipped and the suite reported green.

A test that quietly stops running is worse than one that fails, because
nothing asks why.
"""

from __future__ import annotations

from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]
CONFTEST = TESTS / "conftest.py"
INTEGRATION = TESTS / "integration"

#: Fixtures that genuinely require a running container.
CONTAINER_FIXTURES = (
    "postgres_container",
    "database_url",
    "migrated_database",
    "redis_container",
    "redis_url",
    "redis_client",
)


def _integration_modules() -> list[Path]:
    return sorted(p for p in INTEGRATION.glob("test_*.py"))


class TestTheDockerGateKeysOnTheMarkerNotThePath:
    def test_the_gate_uses_get_closest_marker(self) -> None:
        """The specific regression. ``in item.keywords`` matches ancestor node
        names, so it gates by directory."""
        source = CONFTEST.read_text(encoding="utf-8")
        assert 'get_closest_marker("integration")' in source, (
            'the Docker gate must test the MARKER; `"integration" in '
            "item.keywords` also matches the directory name and silently gates "
            "every test under tests/integration/"
        )

    def test_the_gate_does_not_test_the_keyword(self) -> None:
        """Scans the EXECUTABLE lines only.

        The first version of this test scanned the whole file and failed on the
        conftest docstring, which quotes the old form in order to explain why
        it was wrong. A regression test that cannot tell code from the comment
        describing it is the same crude-scan mistake one level up.
        """
        import ast

        tree = ast.parse(CONFTEST.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or not node.ops:
                continue
            if not isinstance(node.ops[0], ast.In):
                continue
            rendered = ast.unparse(node)
            assert "item.keywords" not in rendered, (
                f"the path-matching form is back as executable code: {rendered}"
            )

    def test_every_container_backed_module_declares_the_marker(self) -> None:
        """With the gate keyed on the marker, a file that needs Docker and
        forgets to declare it no longer skips — it ERRORS on a missing
        container, which is a confusing way to learn the same thing."""
        offenders: list[str] = []
        for path in _integration_modules():
            text = path.read_text(encoding="utf-8")
            uses_container = any(f in text for f in CONTAINER_FIXTURES)
            declares = "pytest.mark.integration" in text
            if uses_container and not declares:
                offenders.append(path.name)
        assert not offenders, (
            "these use a container fixture but do not declare "
            f"pytest.mark.integration, so the Docker gate will not protect "
            f"them: {offenders}"
        )

    def test_a_module_that_needs_no_container_does_not_declare_the_marker(self) -> None:
        """The other direction, and the one that caused this. A marker on a
        pure-computation module hands its coverage to whether Docker happens to
        be running."""
        offenders: list[str] = []
        for path in _integration_modules():
            text = path.read_text(encoding="utf-8")
            uses_container = any(f in text for f in CONTAINER_FIXTURES)
            declares = "pytest.mark.integration" in text
            if declares and not uses_container:
                offenders.append(path.name)
        assert not offenders, (
            "these declare pytest.mark.integration but use no container "
            f"fixture, so they are gated on Docker for no reason: {offenders}"
        )

    def test_the_system_test_specifically_is_not_gated(self) -> None:
        """Named rather than inferred. This is the file whose silent skipping
        was the actual damage, and it is worth failing by name if it ever
        acquires the marker again."""
        path = INTEGRATION / "test_tick_to_trigger.py"
        assert path.exists(), "the system-level test has moved or gone"
        text = path.read_text(encoding="utf-8")
        assert "pytest.mark.integration" not in text
        assert not any(f in text for f in CONTAINER_FIXTURES)


class TestMarkersAreDeclared:
    """`--strict-markers` is on, so an undeclared marker is an error rather
    than a silent no-op. This asserts the ones the suite actually uses are in
    pyproject, because a typo'd marker under strict mode fails loudly at
    collection — but only for the file that uses it."""

    def test_the_integration_marker_is_registered(self) -> None:
        pyproject = (TESTS.parent / "pyproject.toml").read_text(encoding="utf-8")
        assert "integration:" in pyproject

    def test_strict_markers_is_enabled(self) -> None:
        """Without it, `pytest.mark.integraton` (sic) is a silent no-op and the
        file it guards runs against a container that is not there."""
        pyproject = (TESTS.parent / "pyproject.toml").read_text(encoding="utf-8")
        assert "--strict-markers" in pyproject


class TestTheSkipReasonExplainsItself:
    @staticmethod
    def _reason() -> str:
        """The RENDERED message, not the source that produces it.

        The first version of these grepped the file and failed, because the
        literal is wrapped across lines — ``"...are skipped, "`` on one and
        ``"not failed — but..."`` on the next. What a reader actually sees is
        the concatenated value, so that is what to assert on.
        """
        from tests.conftest import _SKIP_REASON

        return _SKIP_REASON

    def test_it_says_the_run_is_not_a_full_green(self) -> None:
        """A skip message that reads like routine noise gets ignored. This one
        has to say the run is incomplete, because that is the decision the
        reader needs to make."""
        assert "not a full green run" in self._reason()

    @pytest.mark.parametrize("cue", ["Docker", "skipped, not failed"])
    def test_it_names_the_cause_and_the_severity(self, cue: str) -> None:
        assert cue in self._reason()
