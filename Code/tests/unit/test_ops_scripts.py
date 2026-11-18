"""The operator scripts report what they checked, and nothing they did not (audit W6).

- ENG-014: the reachability probe read 4 KB of a JSON reply and parsed that, so
  a reachable host serving a longer answer was reported as blocked.
- ENG-041: two probes fetched stand-ins (a landing page, a status API) while
  the summary declared B8 resolved for the endpoints the stories need.
- ENG-042: doctor's .env check looked in the current directory, and on Windows
  judged POSIX mode bits that carry no access control there.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

CODE = Path(__file__).resolve().parents[2]


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"script_{name}", CODE / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered first: @dataclass resolves its class's module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Reply:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body, self.status = body, status

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _Opener:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def open(self, _request: object, timeout: float) -> _Reply:
        return _Reply(self._body)


@pytest.fixture(scope="module")
def reach() -> ModuleType:
    return _script("check_data_reachability")


def _json_probe(reach: ModuleType) -> object:
    return next(p for p in reach.PROBES if p.expect_json)


class TestAJsonReplyIsReadWhole:
    def test_a_20_kb_json_reply_is_a_pass(self, reach: ModuleType) -> None:
        body = json.dumps({"data": ["x" * 100] * 200}).encode()
        assert len(body) > 20_000
        assert reach._probe(_json_probe(reach), _Opener(body)).ok

    def test_the_control_an_html_page_is_not_json(self, reach: ModuleType) -> None:
        result = reach._probe(_json_probe(reach), _Opener(b"<html>Access Denied</html>"))
        assert not result.ok
        assert "not JSON" in result.detail

    def test_a_reply_over_the_cap_is_refused_not_parsed(
        self, reach: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(reach, "MAX_JSON_BYTES", 1024)
        result = reach._probe(_json_probe(reach), _Opener(b"[" + b"1," * 2000 + b"1]"))
        assert not result.ok
        assert "exceeds 1024 bytes" in result.detail


class TestAPassSaysWhatItProved:
    def test_the_stand_in_probes_are_marked_as_stand_ins(self, reach: ModuleType) -> None:
        stand_ins = {p.name for p in reach.PROBES if not p.is_the_endpoint}
        assert stand_ins == {"NSE all-reports page", "NSE market status API"}

    def test_a_clean_run_with_stand_ins_does_not_declare_b8_resolved(
        self, reach: ModuleType
    ) -> None:
        report = reach.Report()
        report.results = [reach.Result(p, True, "HTTP 200") for p in reach.PROBES]
        summary = reach.summary_when_reachable(report)
        assert "B8 is NOT yet resolved" in summary
        assert "NSE all-reports page" in summary and "NSE market status API" in summary

    def test_the_control_only_real_endpoints_resolve_b8(self, reach: ModuleType) -> None:
        report = reach.Report()
        report.results = [
            reach.Result(p, True, "HTTP 200") for p in reach.PROBES if p.is_the_endpoint
        ]
        assert "B8 is resolved for THIS host" in reach.summary_when_reachable(report)


@pytest.fixture(scope="module")
def doctor() -> ModuleType:
    return _script("doctor")


class TestDoctorChecksTheProjectsEnvFile:
    def test_it_is_the_projects_file_wherever_doctor_runs_from(self, doctor: ModuleType) -> None:
        assert doctor.ENV_FILE.is_absolute()
        assert doctor.ENV_FILE == CODE / ".env"

    def test_a_world_readable_file_warns_on_posix(self, doctor: ModuleType, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("X=1\n", encoding="utf-8")
        os.chmod(env, 0o644)
        report = doctor.Report()
        doctor.check_env_file_permissions(report, env, posix=True)
        assert report.warnings == [".env is group/world readable"]

    def test_on_windows_it_says_it_did_not_check(self, doctor: ModuleType, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("X=1\n", encoding="utf-8")
        report = doctor.Report()
        doctor.check_env_file_permissions(report, env, posix=False)
        assert report.warnings == [".env permissions not checked on this platform"]

    def test_no_file_no_finding(self, doctor: ModuleType, tmp_path: Path) -> None:
        report = doctor.Report()
        doctor.check_env_file_permissions(report, tmp_path / ".env", posix=True)
        assert report.warnings == [] and report.failures == []
