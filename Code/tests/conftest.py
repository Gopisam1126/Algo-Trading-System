"""Shared pytest fixtures.

Two things live here: the container fixtures that back the ``integration``
marker, and the guard that makes those tests *skip* rather than *error* when
Docker is not running.

**Why the images are read out of docker-compose.yml.** The integration tests
exist to prove behaviour against the real datastore — TimescaleDB's hypertable
semantics, Redis's ``noeviction`` refusal to write. That proof is worth nothing
if the test container is a different version from the one the system actually
runs on. Rather than restate the pins here and let them drift, both are parsed
from ``ops/docker-compose.yml``, which stays the single source of truth. Change
the pin there and the tests follow automatically.

Gotchas that cost time if you meet them cold, both verified against
testcontainers 4.15:

- ``testcontainers.postgres`` and ``testcontainers.redis`` are **deprecated**
  import paths and emit a ``DeprecationWarning``. The current paths are
  ``testcontainers.community.postgres`` / ``.redis``.
- ``PostgresContainer`` defaults to ``driver="psycopg2"``, which this project
  does not install — it uses psycopg **3**. Left at the default,
  ``get_connection_url()`` hands back a ``postgresql+psycopg2://`` URL and
  every connection fails with ``ModuleNotFoundError``. Hence the explicit
  ``driver="psycopg"``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

if TYPE_CHECKING:  # pragma: no cover
    from redis import Redis

CODE_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = CODE_ROOT / "ops" / "docker-compose.yml"


# ---------------------------------------------------------------------------
# Pin discovery — one source of truth, shared with production
# ---------------------------------------------------------------------------


def _compose_image(service: str, fallback: str) -> str:
    """Read a service's pinned image from docker-compose.yml.

    Falls back rather than raising: a missing compose file should not stop the
    unit tests from running.
    """
    try:
        spec: dict[str, Any] = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
        image = spec["services"][service]["image"]
        return str(image)
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        return fallback


TIMESCALE_IMAGE = os.environ.get(
    "TEST_TIMESCALE_IMAGE",
    _compose_image("timescaledb", "timescale/timescaledb:2.17.2-pg16"),
)
REDIS_IMAGE = os.environ.get(
    "TEST_REDIS_IMAGE",
    _compose_image("redis", "redis:7.4-alpine"),
)


# ---------------------------------------------------------------------------
# Docker availability — skip, do not error
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """True when a Docker daemon is actually reachable.

    Checked once per session. Import of the docker SDK alone is not enough —
    the package is installed as a testcontainers dependency whether or not the
    daemon is running, and on Windows the daemon stops whenever Docker Desktop
    is closed.
    """
    try:
        import docker
    except ImportError:
        return False
    try:
        docker.from_env().ping()
    except Exception:
        return False
    return True


DOCKER_AVAILABLE = _docker_available()

_SKIP_REASON = (
    "Docker is not running, so the integration tests cannot start their "
    "containers. Start Docker Desktop and re-run. These tests are skipped, "
    "not failed — but they are the ones that prove the datastore behaviour, "
    "so a green run without them is not a full green run."
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip anything marked ``integration`` when Docker is unreachable."""
    if DOCKER_AVAILABLE:
        return
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Say up front whether integration coverage is actually running."""
    if DOCKER_AVAILABLE:
        return [f"docker: available — timescale={TIMESCALE_IMAGE} redis={REDIS_IMAGE}"]
    return ["docker: NOT running — integration tests will be SKIPPED"]


# ---------------------------------------------------------------------------
# Container fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[Any]:
    """A TimescaleDB container for the whole test session.

    Session-scoped because container startup dominates the runtime of any
    single test. Tests that need isolation should roll back their transaction
    or use the ``clean_db`` fixture rather than asking for a fresh container.
    """
    if not DOCKER_AVAILABLE:
        pytest.skip(_SKIP_REASON)

    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer(
        image=TIMESCALE_IMAGE,
        username="algotrader",
        password="test_only_not_a_real_secret",
        dbname="algotrader_test",
        driver="psycopg",  # NOT the psycopg2 default — see module docstring
    )
    with container:
        yield container


@pytest.fixture(scope="session")
def database_url(postgres_container: Any) -> str:
    """SQLAlchemy async URL for the test database, with TimescaleDB enabled.

    The extension is created here rather than in a migration because it needs
    superuser rights, which the application role deliberately does not have in
    production — there the DBA (you) runs it once at provisioning time.
    """
    url: str = postgres_container.get_connection_url()

    import psycopg

    # psycopg connects with its own URL scheme, not SQLAlchemy's.
    with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://")) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;")
        conn.commit()
    return url


@pytest.fixture(scope="session")
def redis_container() -> Iterator[Any]:
    """A Redis container matching the production pin.

    Started with the same ``--maxmemory-policy noeviction`` the real server
    uses. That policy is the reason ``maxlen`` is mandatory on stream
    publication (E01-S04): under ``noeviction`` a full stream makes Redis
    *refuse writes* rather than silently discard old entries. A test container
    running the default policy would quietly hide exactly the failure these
    tests exist to catch.
    """
    if not DOCKER_AVAILABLE:
        pytest.skip(_SKIP_REASON)

    from testcontainers.community.redis import RedisContainer

    container = RedisContainer(image=REDIS_IMAGE)
    container.with_command(
        "redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy noeviction"
    )
    with container:
        yield container


@pytest.fixture(scope="session")
def redis_url(redis_container: Any) -> str:
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest.fixture
def redis_client(redis_container: Any) -> Iterator[Redis]:
    """A Redis client, flushed after each test so state cannot leak sideways."""
    client = redis_container.get_client(decode_responses=True)
    try:
        yield client
    finally:
        client.flushall()
        client.close()


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> Iterator[str]:
    """The test database with ``alembic upgrade head`` applied.

    Points Alembic at the container by setting ``DATABASE_URL``, which
    ``migrations/env.py`` treats as an absolute override. The previous value is
    restored afterwards so this cannot leak into another test's environment.
    """
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        alembic_cfg = Config(str(CODE_ROOT / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(CODE_ROOT / "migrations"))
        command.upgrade(alembic_cfg, "head")
        yield database_url
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture
def anyio_backend() -> str:
    """Pin async tests to asyncio; the project does not use trio."""
    return "asyncio"
