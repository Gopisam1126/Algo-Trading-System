"""Secret handling.

Five rules, in priority order (LOW_LEVEL_ARCHITECTURE.md §10.4):

1. No secret in source control, ever.
2. No secret in a config file — config holds a *reference*.
3. No secret in logs.
4. No secret in an LLM prompt.
5. No secret in an error message or notification.

:class:`SecretString` enforces rules 3–5 mechanically.  It overrides
``__repr__``, ``__str__``, and ``__format__`` to return a redaction marker, so
an accidental ``print()``, f-string, or exception message cannot leak the
value.  Reading the real value requires an explicit, auditable
``.reveal()`` call.
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from typing import Any, Final

REDACTED: Final = "***REDACTED***"


class SecretString:
    """A string that refuses to render itself.

    >>> s = SecretString("hunter2", name="broker_password")
    >>> print(s)
    ***REDACTED***
    >>> f"password is {s}"
    'password is ***REDACTED***'
    >>> s.reveal()
    'hunter2'
    """

    __slots__ = ("_name", "_value")

    def __init__(self, value: str, *, name: str = "secret") -> None:
        self._value = value
        self._name = name

    # -- Rendering is always redacted --------------------------------------

    def __repr__(self) -> str:
        return f"SecretString({self._name}={REDACTED})"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, _spec: str) -> str:
        return REDACTED

    # Block accidental serialization paths
    def __reduce__(self) -> Any:
        raise TypeError("SecretString must not be pickled")

    def __getstate__(self) -> Any:
        raise TypeError("SecretString must not be serialized")

    # -- Comparison without leaking ----------------------------------------

    def __eq__(self, other: object) -> bool:
        import hmac

        if isinstance(other, SecretString):
            return hmac.compare_digest(self._value, other._value)
        if isinstance(other, str):
            return hmac.compare_digest(self._value, other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("SecretString", self._name))

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    # -- Explicit access ---------------------------------------------------

    def reveal(self) -> str:
        """Return the underlying value.

        Call this only at the point of use (an HTTP header, an SDK client
        constructor).  Never assign the result to a long-lived variable, and
        never pass it anywhere that might log it.
        """
        return self._value

    @property
    def name(self) -> str:
        return self._name


class SecretNotFoundError(KeyError):
    pass


class SecretsProvider(ABC):
    """Interface shared by every backend, so migrating from env → SOPS →
    Vault is a config change rather than a refactor."""

    @abstractmethod
    def get(self, key: str) -> SecretString: ...

    def get_optional(self, key: str) -> SecretString | None:
        try:
            return self.get(key)
        except SecretNotFoundError:
            return None

    def require_all(self, keys: list[str]) -> dict[str, SecretString]:
        """Fetch several secrets, reporting ALL missing ones at once.

        Failing on the first missing key means discovering a misconfiguration
        one restart at a time — unhelpful at 07:00 before the open.
        """
        found: dict[str, SecretString] = {}
        missing: list[str] = []
        for key in keys:
            try:
                found[key] = self.get(key)
            except SecretNotFoundError:
                missing.append(key)
        if missing:
            raise SecretNotFoundError(f"missing required secrets: {', '.join(sorted(missing))}")
        return found

    def known_values(self) -> set[str]:
        """Every loaded secret value, for the log redaction filter.

        Deliberately returns raw strings — this is consumed only by the
        logging filter, which needs them to scrub matches.
        """
        return set()


class EnvSecretsProvider(SecretsProvider):
    """Reads from environment variables.  Development and small deployments."""

    def __init__(self) -> None:
        self._cache: dict[str, SecretString] = {}

    def get(self, key: str) -> SecretString:
        if key in self._cache:
            return self._cache[key]
        raw = os.environ.get(key)
        if raw is None or raw == "":
            raise SecretNotFoundError(key)
        secret = SecretString(raw, name=key)
        self._cache[key] = secret
        return secret

    def known_values(self) -> set[str]:
        return {s.reveal() for s in self._cache.values() if len(s.reveal()) >= 8}


class SopsSecretsProvider(SecretsProvider):
    """Decrypts a SOPS+age encrypted YAML file.

    The pragmatic stepping stone before running Vault: encrypted secrets can
    live in git safely, with the age private key present only on the
    production host.
    """

    def __init__(self, path: str, age_key_file: str | None = None) -> None:
        self._path = path
        self._age_key_file = age_key_file
        self._data: dict[str, str] | None = None
        self._cache: dict[str, SecretString] = {}

    def _load(self) -> dict[str, str]:
        if self._data is not None:
            return self._data
        import subprocess

        env = dict(os.environ)
        if self._age_key_file:
            env["SOPS_AGE_KEY_FILE"] = self._age_key_file
        # Resolve the absolute path rather than letting subprocess search PATH
        # at exec time. A partial name means whatever `sops` PATH happens to
        # resolve to gets to read the decryption key — and this process holds
        # SOPS_AGE_KEY_FILE. Resolving explicitly makes the binary being run an
        # observable fact rather than an ambient one.
        sops = shutil.which("sops")
        if sops is None:
            raise RuntimeError("sops binary not found on PATH")

        try:
            result = subprocess.run(  # noqa: S603 — absolute path, fixed argv, shell=False
                [sops, "--decrypt", "--output-type", "json", self._path],
                capture_output=True,
                check=True,
                env=env,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("sops binary not found on PATH") from exc
        except subprocess.CalledProcessError as exc:
            # Deliberately does NOT include stderr — it can echo secret material.
            raise RuntimeError(f"sops decryption failed (exit {exc.returncode})") from None

        import json

        self._data = json.loads(result.stdout)
        return self._data

    def get(self, key: str) -> SecretString:
        if key in self._cache:
            return self._cache[key]
        data = self._load()
        if key not in data:
            raise SecretNotFoundError(key)
        secret = SecretString(str(data[key]), name=key)
        self._cache[key] = secret
        return secret

    def known_values(self) -> set[str]:
        data = self._load()
        return {str(v) for v in data.values() if len(str(v)) >= 8}


def build_provider(kind: str | None = None) -> SecretsProvider:
    """Factory driven by the ``SECRETS_PROVIDER`` environment variable."""
    kind = (kind or os.environ.get("SECRETS_PROVIDER", "env")).lower()
    if kind == "env":
        return EnvSecretsProvider()
    if kind == "sops":
        path = os.environ.get("SOPS_FILE", "config/secrets.sops.yaml")
        return SopsSecretsProvider(path, os.environ.get("SOPS_AGE_KEY_FILE"))
    if kind == "vault":
        raise NotImplementedError(
            "Vault provider not yet implemented — see LOW_LEVEL_ARCHITECTURE.md §17 D1"
        )
    raise ValueError(f"unknown secrets provider: {kind!r}")
