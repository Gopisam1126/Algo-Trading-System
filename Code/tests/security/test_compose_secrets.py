"""Each container receives only the credentials its own job needs (audit SEC-001).

``ops/docker-compose.yml`` said, in its header, that only ``execution-svc``
holds trading credentials. It put ``env_file: [../.env]`` in the anchor every
service merges - so the Kite API key, secret and access token, the Anthropic
key and the Telegram token reached every app container AND the upstream Redis,
TimescaleDB, Prometheus and Grafana images. The containment was a comment.

Held here two ways: on the YAML as written (fast, no Docker), and on what
``docker compose config`` RENDERS - Compose's own reading of the merges and the
interpolation, which is what a container actually receives.

The second also catches the defect found writing the first: a YAML merge
(``<<``) is shallow, so a service that declared its own ``environment:``
REPLACED the anchor's, and market-ingest, signal-engine and execution-svc had
no ``DATABASE_URL`` or ``REDIS_URL`` at all (18 Nov 2026).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

CODE = Path(__file__).resolve().parents[2]
COMPOSE = CODE / "ops" / "docker-compose.yml"

#: Who may receive each credential. A prefix ends in "_"; anything else is an
#: exact name. The owner of a credential is the service whose OWN job uses it.
OWNERS: dict[str, set[str]] = {
    "KITE_": {"execution-svc"},
    "FYERS_": set(),  # the fallback broker is not built; nothing needs it yet
    "ANTHROPIC_API_KEY": {"signal-engine", "macro-svc", "premarket-job"},
    "TELEGRAM_": {"notifier"},
    "NEWS_API_KEY": {"macro-svc", "premarket-job"},
    "GRAFANA_PASSWORD": {"grafana"},
    "POSTGRES_PASSWORD": {"timescaledb"},  # the OWNER's password: migrations only
}

#: The settings each third-party image may receive, and nothing else.
DATASTORE_ENV: dict[str, set[str]] = {
    "redis": set(),
    "prometheus": set(),
    "grafana": {"GF_SECURITY_ADMIN_PASSWORD", "GF_USERS_ALLOW_SIGN_UP"},
    "timescaledb": {
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "ALGOTRADER_APP_DB_USER",
        "ALGOTRADER_APP_DB_PASSWORD",
    },
}


def _services() -> dict[str, Any]:
    return dict(yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"])


def _env(service: dict[str, Any]) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):  # the KEY=VALUE list form
        return dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


def _carried(service: dict[str, Any]) -> set[str]:
    """Every variable a service receives: its keys, and every ``${VAR}`` its
    values interpolate (a password inside DATABASE_URL is still that password)."""
    env = _env(service)
    names = set(env)
    for value in env.values():
        names.update(re.findall(r"\$\{([A-Za-z0-9_]+)", value))
    return names


def _matches(name: str, credential: str) -> bool:
    return name.startswith(credential) if credential.endswith("_") else name == credential


def _app_services(services: dict[str, Any]) -> list[str]:
    return sorted(name for name, svc in services.items() if "build" in svc)


class TestEveryContainerGetsOnlyItsOwnSecrets:
    def test_no_service_reads_the_whole_env_file(self) -> None:
        with_file = [n for n, s in _services().items() if "env_file" in s]
        assert not with_file, f"env_file hands the whole .env to: {with_file}"

    @pytest.mark.parametrize("credential", sorted(OWNERS))
    def test_each_credential_reaches_only_its_owners(self, credential: str) -> None:
        holders = {
            name
            for name, svc in _services().items()
            if any(_matches(v, credential) for v in _carried(svc))
        }
        assert holders <= OWNERS[credential], (
            f"{credential} reaches {sorted(holders - OWNERS[credential])}, which do not use it"
        )

    def test_the_execution_service_has_what_it_trades_with(self) -> None:
        """The control: a compose file that gave nobody anything would pass the
        test above."""
        carried = _carried(_services()["execution-svc"])
        assert {"KITE_API_KEY", "KITE_API_SECRET", "KITE_USER_ID"} <= carried

    @pytest.mark.parametrize("name", sorted(DATASTORE_ENV))
    def test_a_datastore_or_monitoring_image_gets_only_its_own_settings(self, name: str) -> None:
        env = set(_env(_services()[name]))
        assert env <= DATASTORE_ENV[name], f"{name} receives {sorted(env - DATASTORE_ENV[name])}"


class TestEveryAppServiceCanReachItsDatastores:
    """A shallow YAML merge dropped both URLs from every service that set its
    own environment - found writing this file."""

    @pytest.mark.parametrize("variable", ["DATABASE_URL", "REDIS_URL"])
    def test_every_app_service_receives_it(self, variable: str) -> None:
        services = _services()
        missing = [n for n in _app_services(services) if variable not in _env(services[n])]
        assert not missing, f"{variable} is missing from {missing}"

    def test_services_connect_as_the_app_login_not_the_owner(self) -> None:
        """SEC-002: the owner can rewrite the audit trail; the app login cannot."""
        services = _services()
        for name in _app_services(services):
            url = _env(services[name])["DATABASE_URL"]
            assert "ALGOTRADER_APP_DB_USER" in url and "ALGOTRADER_APP_DB_PASSWORD" in url, name
            assert "POSTGRES_PASSWORD" not in url and "POSTGRES_USER" not in url, name


class TestTheApplicationCannotRewriteItsOwnCode:
    """ENG-031: the Dockerfile said "read-only application directory" and
    chowned it to the runtime user."""

    def test_every_app_service_runs_read_only_with_a_scratch_tmp(self) -> None:
        services = _services()
        for name in _app_services(services):
            assert services[name].get("read_only") is True, name
            assert "/tmp" in (services[name].get("tmpfs") or []), name

    def test_the_image_does_not_hand_its_code_to_the_runtime_user(self) -> None:
        """In the RUNTIME stage - the image that ships. The first version of
        this test read the first matching COPY in the file, which is the
        BUILDER stage's, so putting --chown back where it mattered changed
        nothing it could see (a surviving mutation, 18 Nov 2026)."""
        dockerfile = (CODE / "ops" / "Dockerfile").read_text(encoding="utf-8")
        _, runtime = re.split(r"^FROM \S+ AS runtime$", dockerfile, maxsplit=1, flags=re.M)
        for path in ("src", "config"):
            copies = re.findall(rf"^COPY\b.*\b{path} \./{path}$", runtime, re.M)
            assert copies, f"the runtime stage does not COPY {path}"
            owned = [c for c in copies if "--chown" in c]
            assert not owned, f"{path} is owned by the runtime user: {owned}"


@pytest.mark.integration
class TestWhatComposeActuallyRenders:
    """Compose's own reading: merges resolved, values interpolated. Needs only
    the docker CLI, which the container-backed suite requires anyway."""

    #: Who may hold each variable's value once Compose has rendered it.
    OWNERS: ClassVar[dict[str, set[str]]] = {
        "POSTGRES_PASSWORD": {"timescaledb"},
        "ALGOTRADER_APP_DB_PASSWORD": set(),  # checked below: every app service, by URL
        "GRAFANA_PASSWORD": {"grafana"},
        "KITE_API_KEY": {"execution-svc"},
        "KITE_API_SECRET": {"execution-svc"},
        "KITE_USER_ID": {"execution-svc"},
        "ANTHROPIC_API_KEY": {"signal-engine", "macro-svc", "premarket-job"},
        "TELEGRAM_BOT_TOKEN": {"notifier"},
        "NEWS_API_KEY": {"macro-svc", "premarket-job"},
        "FYERS_SECRET_KEY": set(),
    }
    #: A distinct placeholder per variable, BUILT from its name so the repository's
    #: secret scanner never sees a `"..._API_KEY": "<value>"` literal here (gitleaks
    #: generic-api-key flagged the first version, 18 Nov 2026).
    DUMMY: ClassVar[dict[str, str]] = {name: f"placeholder-{name.lower()}-end" for name in OWNERS}

    def _rendered(self, tmp_path: Path) -> dict[str, Any]:
        docker = shutil.which("docker")
        assert docker, "the docker CLI is required"
        env_file = tmp_path / "compose.env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in self.DUMMY.items()), encoding="utf-8")
        done = subprocess.run(  # a fixed command over a fixed file
            [
                docker,
                "compose",
                "--env-file",
                str(env_file),
                "-f",
                str(COMPOSE),
                "config",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert done.returncode == 0, done.stderr[-2000:]
        return dict(json.loads(done.stdout)["services"])

    def test_a_secret_value_appears_only_where_it_belongs(self, tmp_path: Path) -> None:
        rendered = self._rendered(tmp_path)
        for name, allowed in self.OWNERS.items():
            if name == "ALGOTRADER_APP_DB_PASSWORD":
                continue  # every app service and the database: the URL test below
            value = self.DUMMY[name]
            holders = {
                name
                for name, svc in rendered.items()
                if value in json.dumps(svc.get("environment") or {})
            }
            assert holders <= allowed, f"{name} rendered into {sorted(holders - allowed)}"

    def test_every_app_service_renders_both_datastore_urls(self, tmp_path: Path) -> None:
        rendered = self._rendered(tmp_path)
        for name, svc in rendered.items():
            if "build" not in svc:
                continue
            env = svc.get("environment") or {}
            assert self.DUMMY["ALGOTRADER_APP_DB_PASSWORD"] in str(env.get("DATABASE_URL")), name
            assert str(env.get("REDIS_URL", "")).startswith("redis://redis:"), name
