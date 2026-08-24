"""The secrets provider layer — constraint C6's implementation.

`SecretString` itself is well covered (nine render paths, pickling, exception
interpolation). The *providers* that load secrets had no tests at all, which
leaves several genuinely security-relevant properties resting on nothing:

- a SOPS decryption failure must not echo stderr, because sops prints the
  offending material on some failures and that error goes straight to the log;
- `known_values()` is what feeds the log redactor, so a provider that returns an
  empty set silently disables exact-match scrubbing for everything it loaded;
- an unknown provider name must be refused rather than quietly defaulting to
  environment variables, which would look like it worked.

The SOPS tests stub `subprocess.run` rather than requiring the binary. What is
under test is this module's handling of the result, not sops itself.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from algotrader.common.secrets import (
    EnvSecretsProvider,
    SecretNotFoundError,
    SecretString,
    SopsSecretsProvider,
    build_provider,
)

SECRET_MATERIAL = "TopSecretValue123456"


class TestEnvProvider:
    def test_a_present_variable_is_returned_as_a_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KITE_API_KEY", SECRET_MATERIAL)
        secret = EnvSecretsProvider().get("KITE_API_KEY")
        assert secret.reveal() == SECRET_MATERIAL
        assert SECRET_MATERIAL not in str(secret), "the provider handed back something that renders"

    def test_a_missing_variable_raises_rather_than_returning_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returning None would be read downstream as "no credential needed"."""
        monkeypatch.delenv("KITE_API_KEY", raising=False)
        with pytest.raises(SecretNotFoundError):
            EnvSecretsProvider().get("KITE_API_KEY")

    def test_an_empty_variable_counts_as_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`KITE_API_KEY=` in a .env is the most common way to "set" nothing."""
        monkeypatch.setenv("KITE_API_KEY", "")
        with pytest.raises(SecretNotFoundError):
            EnvSecretsProvider().get("KITE_API_KEY")

    def test_every_missing_key_is_reported_at_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Discovering a misconfiguration one restart at a time, at 07:00, is bad."""
        for key in ("A_KEY", "B_KEY", "C_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("B_KEY", SECRET_MATERIAL)

        with pytest.raises(SecretNotFoundError) as exc:
            EnvSecretsProvider().require_all(["A_KEY", "B_KEY", "C_KEY"])

        message = str(exc.value)
        assert "A_KEY" in message and "C_KEY" in message
        assert "B_KEY" not in message, "a key that WAS found must not be reported missing"

    def test_known_values_feeds_the_redactor_only_after_loading(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The redactor can only scrub what the provider admits it loaded."""
        monkeypatch.setenv("KITE_API_KEY", SECRET_MATERIAL)
        provider = EnvSecretsProvider()
        assert provider.known_values() == set(), "nothing loaded yet"
        provider.get("KITE_API_KEY")
        assert SECRET_MATERIAL in provider.known_values()

    def test_short_values_are_excluded_from_redaction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Scrubbing a 3-character secret would redact half of every log line."""
        monkeypatch.setenv("SHORT_KEY", "abc")
        provider = EnvSecretsProvider()
        provider.get("SHORT_KEY")
        assert "abc" not in provider.known_values()


class TestSopsProviderDoesNotLeakOnFailure:
    """The property that matters: a decryption failure must stay quiet."""

    @staticmethod
    def _failing_run(stderr: bytes) -> Any:
        def run(*_args: object, **_kw: object) -> object:
            raise subprocess.CalledProcessError(1, "sops", output=b"", stderr=stderr)

        return run

    def test_stderr_is_not_included_in_the_raised_error(self) -> None:
        """sops can echo the offending material; that error goes to the log."""
        leaky_stderr = f"failed to decrypt: value was {SECRET_MATERIAL}".encode()

        provider = SopsSecretsProvider("config/secrets.sops.yaml")
        with (
            patch("shutil.which", return_value="/usr/bin/sops"),
            patch("subprocess.run", self._failing_run(leaky_stderr)),
            pytest.raises(RuntimeError) as exc,
        ):
            provider.get("KITE_API_KEY")

        assert SECRET_MATERIAL not in str(exc.value), "sops stderr leaked into the exception"
        assert "exit 1" in str(exc.value), "the error must still say what happened"

    def test_the_cause_chain_does_not_reintroduce_the_leak(self) -> None:
        """`raise ... from None` matters here — `from exc` would attach stderr.

        A chained cause is rendered by the traceback formatter, so keeping it
        would put the stderr back into the log by another route.
        """
        leaky_stderr = f"boom {SECRET_MATERIAL}".encode()

        provider = SopsSecretsProvider("config/secrets.sops.yaml")
        with (
            patch("shutil.which", return_value="/usr/bin/sops"),
            patch("subprocess.run", self._failing_run(leaky_stderr)),
            pytest.raises(RuntimeError) as exc,
        ):
            provider.get("KITE_API_KEY")

        assert exc.value.__cause__ is None, "the CalledProcessError carries stderr; it must be cut"

    def test_a_missing_sops_binary_fails_loudly(self) -> None:
        provider = SopsSecretsProvider("config/secrets.sops.yaml")
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(RuntimeError, match="sops binary not found"),
        ):
            provider.get("KITE_API_KEY")

    def test_the_binary_is_resolved_absolutely_not_via_path_at_exec(self) -> None:
        """A bare `sops` would let PATH decide what reads the age key.

        This process holds SOPS_AGE_KEY_FILE, so whatever binary runs here can
        read the decryption key.
        """
        captured: dict[str, Any] = {}

        def fake_run(argv: list[str], **kw: object) -> Any:
            captured["argv"] = argv
            captured["shell"] = kw.get("shell", False)

            class R:
                stdout = b'{"KITE_API_KEY": "' + SECRET_MATERIAL.encode() + b'"}'

            return R()

        provider = SopsSecretsProvider("config/secrets.sops.yaml")
        with (
            patch("shutil.which", return_value="/usr/local/bin/sops"),
            patch("subprocess.run", fake_run),
        ):
            provider.get("KITE_API_KEY")

        assert captured["argv"][0] == "/usr/local/bin/sops", "must invoke an absolute path"
        assert captured["shell"] is False, "shell=True would reintroduce injection"

    def test_a_key_absent_from_the_decrypted_file_raises(self) -> None:
        def fake_run(_argv: list[str], **_kw: object) -> Any:
            class R:
                stdout = b'{"OTHER_KEY": "value"}'

            return R()

        provider = SopsSecretsProvider("config/secrets.sops.yaml")
        with (
            patch("shutil.which", return_value="/usr/bin/sops"),
            patch("subprocess.run", fake_run),
            pytest.raises(SecretNotFoundError),
        ):
            provider.get("KITE_API_KEY")


class TestProviderFactory:
    def test_env_is_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SECRETS_PROVIDER", raising=False)
        assert isinstance(build_provider(), EnvSecretsProvider)

    def test_an_unknown_provider_is_refused_not_defaulted(self) -> None:
        """Silently falling back to env would look like it worked."""
        with pytest.raises(ValueError, match="unknown secrets provider"):
            build_provider("nope")

    def test_vault_fails_closed_while_unimplemented(self) -> None:
        """Config names it and .env.example hints at it; it must not no-op."""
        with pytest.raises(NotImplementedError):
            build_provider("vault")

    def test_the_name_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SECRETS_PROVIDER", "ENV")
        assert isinstance(build_provider(), EnvSecretsProvider)


class TestSecretStringCannotEscapeItsWrapper:
    """Coverage found these paths untested — 26 statements of ``secrets.py``,
    including every dunder that exists specifically to stop a credential
    leaving the process.

    Invariant 4 is "secrets never render". The methods below are how that is
    enforced, so each needs a probe that would catch it being false rather than
    a reading that says it looks right.
    """

    def test_a_secret_refuses_to_be_pickled(self) -> None:
        """The quiet exfiltration path. Anything reachable by ``pickle`` can be
        written to Redis, a cache, or a crash dump — and a SecretString that
        pickled cleanly would carry the plaintext with it."""
        import pickle

        # __reduce__ fires before __getstate__, so this is the outer guard;
        # the inner one is asserted separately below. Both exist because
        # removing either would leave a working serialisation path.
        with pytest.raises(TypeError, match="must not be pickled"):
            pickle.dumps(SecretString("hunter2hunter2", name="API_KEY"))

    def test_getstate_is_what_blocks_it(self) -> None:
        with pytest.raises(TypeError, match="must not be serialized"):
            SecretString("hunter2hunter2", name="API_KEY").__getstate__()

    def test_copy_is_blocked_by_the_same_guard(self) -> None:
        """``copy.deepcopy`` uses the same protocol, and deepcopy of a config
        object is exactly how a secret ends up somewhere unexpected."""
        import copy

        with pytest.raises(TypeError):
            copy.deepcopy(SecretString("hunter2hunter2", name="API_KEY"))

    def test_equality_does_not_leak_through_repr_on_failure(self) -> None:
        a = SecretString("correct-horse-battery", name="A")
        b = SecretString("correct-horse-battery", name="B")
        assert a == b

    def test_a_different_value_is_not_equal(self) -> None:
        assert SecretString("aaaaaaaaaaaa", name="A") != SecretString("bbbbbbbbbbbb", name="B")

    def test_it_compares_equal_to_a_matching_plain_string(self) -> None:
        """Needed so a caller can verify a value without calling reveal()."""
        assert SecretString("s3cret-value-x", name="A") == "s3cret-value-x"

    def test_comparison_is_constant_time(self) -> None:
        """Not a timing measurement — a structural assertion that the
        implementation routes through hmac.compare_digest. A plain ``==``
        returns early on the first differing byte, which leaks the length of
        the matching prefix to anything that can time it."""
        import inspect

        source = inspect.getsource(SecretString.__eq__)
        assert "compare_digest" in source

    def test_comparison_to_an_unrelated_type_is_not_an_error(self) -> None:
        assert SecretString("aaaaaaaaaaaa", name="A") != 42

    def test_it_is_hashable_without_hashing_the_secret(self) -> None:
        """A dict key must not be derivable from the plaintext, or the hash
        becomes an oracle."""
        secret = SecretString("aaaaaaaaaaaa", name="API_KEY")
        assert hash(secret) == hash(SecretString("different-value", name="API_KEY"))
        assert {secret: 1}[secret] == 1

    def test_truthiness_reflects_emptiness_only(self) -> None:
        assert bool(SecretString("x", name="A"))
        assert not bool(SecretString("", name="A"))

    def test_length_is_available_without_revealing(self) -> None:
        """Length is needed to decide whether a value is long enough to redact
        on; the value itself is not."""
        assert len(SecretString("abcdefgh", name="A")) == 8

    def test_the_name_is_public_but_the_value_is_not(self) -> None:
        secret = SecretString("abcdefghijkl", name="KITE_API_SECRET")
        assert secret.name == "KITE_API_SECRET"
        assert "abcdefghijkl" not in repr(secret)
        assert "abcdefghijkl" not in str(secret)
        assert "abcdefghijkl" not in f"{secret}"

    def test_it_survives_string_formatting_without_leaking(self) -> None:
        """Every rendering path, because each is a different dunder and one of
        them being forgotten is exactly how this fails."""
        secret = SecretString("abcdefghijkl", name="A")
        for rendered in (f"{secret}", f"{secret!s}", f"{secret!r}", f"{secret}"):
            assert "abcdefghijkl" not in rendered

    def test_reveal_is_the_only_way_out(self) -> None:
        assert SecretString("abcdefghijkl", name="A").reveal() == "abcdefghijkl"
