"""Proves the Sprint 1 scaffolding works before any E01 story is built on it.

These are not tests of business logic — there is none yet. They verify the
things E01-S01..S04 will assume on day one, so that a failure there is a
failure in the story's own code rather than in the ground it stands on.

Each assertion corresponds to a claim made in EPIC01_TECHNICAL_SPEC.md or in
the setup docs. Per the repo's working rule, a claim gets a probe that fails
when the claim is false.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]


class TestTimescaleContainer:
    def test_timescaledb_extension_is_available(self, database_url: str) -> None:
        """The `timescaledb` extension loads — without it there are no hypertables."""
        import psycopg

        dsn = database_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"
            ).fetchone()

        assert row is not None, (
            "the timescaledb extension is not installed in the test database; "
            "hypertable creation in E01-S01 would fail"
        )

    def test_columnstore_api_matches_the_pinned_version(self, database_url: str) -> None:
        """Which compression API exists depends on the pin — assert they agree.

        `add_columnstore_policy` arrived in TimescaleDB 2.18 (hypercore). The
        pin decides which of the two DDL forms E01-S01 must write, and getting
        it wrong is only discovered when the first migration runs. This test
        turns that into an explicit, readable statement of which world we are
        in, and fails loudly if the pin moves across the 2.18 boundary without
        the migrations being revisited.
        """
        import psycopg

        dsn = database_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(dsn) as conn:
            version_row = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"
            ).fetchone()
            assert version_row is not None
            version = str(version_row[0])

            has_columnstore = conn.execute(
                "SELECT COUNT(*) FROM pg_proc WHERE proname = 'add_columnstore_policy'"
            ).fetchone()
            assert has_columnstore is not None

        major, minor, *_ = (int(p) for p in version.split(".")[:2])
        expects_columnstore = (major, minor) >= (2, 18)
        actually_has = bool(has_columnstore[0])

        assert actually_has == expects_columnstore, (
            f"TimescaleDB {version} "
            f"{'should' if expects_columnstore else 'should not'} expose "
            f"add_columnstore_policy, but "
            f"{'it does not' if expects_columnstore else 'it does'}. "
            f"Re-check which compression DDL the migrations use."
        )


class TestRedisContainer:
    def test_noeviction_policy_is_active(self, redis_client) -> None:
        """The container must mirror production's `noeviction`.

        If this drifts to a default eviction policy, the E01-S04 test that
        proves an unbounded stream is refused would pass for the wrong reason —
        Redis would silently evict instead of refusing, and the missing `maxlen`
        would go unnoticed until it filled memory in production.
        """
        policy = redis_client.config_get("maxmemory-policy")["maxmemory-policy"]
        assert policy == "noeviction", (
            f"test Redis is running maxmemory-policy={policy!r}, but production "
            f"runs 'noeviction'. See ops/docker-compose.yml."
        )

    def test_client_round_trips(self, redis_client) -> None:
        redis_client.set("scaffold:probe", "ok")
        assert redis_client.get("scaffold:probe") == "ok"

    def test_state_does_not_leak_between_tests(self, redis_client) -> None:
        """Depends on the previous test having run — the flush must have happened."""
        assert redis_client.get("scaffold:probe") is None


class TestAlembicWiring:
    def test_migrations_apply_and_fully_reverse(self, database_url: str) -> None:
        """upgrade head -> downgrade base -> upgrade head, clean.

        An E01-S01 acceptance criterion, asserted here from the first day so it
        cannot quietly rot as migrations accumulate. With no migrations written
        yet this is trivially true; it stops being trivial the moment E01-S01
        lands, which is exactly when it starts earning its keep.
        """
        import os

        from alembic import command
        from alembic.config import Config

        from tests.conftest import CODE_ROOT

        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = database_url
        try:
            cfg = Config(str(CODE_ROOT / "alembic.ini"))
            cfg.set_main_option("script_location", str(CODE_ROOT / "migrations"))
            command.upgrade(cfg, "head")
            command.downgrade(cfg, "base")
            command.upgrade(cfg, "head")
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous
