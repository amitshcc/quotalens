"""Session cookie storage and redaction.

The cookie is the one thing this project must never leak. Two defences live here:

* :class:`KeyringSecretStore` is the *only* persistence path for the cookie.
* :class:`Redactor` scrubs the cookie value (and anything that looks like one)
  from log records and error strings, so a careless ``str(exc)`` cannot expose it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Protocol

KEYRING_SERVICE = "quotalens"
KEYRING_USERNAME = "claude.ai-session-cookie"


def keyring_username(profile: str = "") -> str:
    """One keyring entry per profile, so two accounts never share a cookie."""
    return f"{KEYRING_USERNAME}:{profile}" if profile else KEYRING_USERNAME


REDACTED = "[REDACTED]"
# Cookie pairs whose value is an identifier, not a credential; redacting them would
# hide the org id from every error message and make endpoint drift undebuggable.
NON_SECRET_COOKIE_NAMES = frozenset({"lastActiveOrg"})

# Patterns that catch cookie material even when we do not know the exact value:
# a sessionKey pair, a Cookie header, or an Anthropic session token prefix.
_GENERIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(sessionKey=)[^;\s'\"]+", re.IGNORECASE),
    re.compile(r"(\bcookie['\"]?\s*[:=]\s*)['\"]?[^'\"\r\n]+", re.IGNORECASE),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
)


class SecretStore(Protocol):
    """Where the cookie lives. Tests inject an in-memory implementation."""

    def get_cookie(self) -> str | None: ...

    def set_cookie(self, value: str) -> None: ...

    def delete_cookie(self) -> None: ...


class SecretStoreError(RuntimeError):
    """The OS keyring is unavailable or refused the operation."""


class KeyringSecretStore:
    """Cookie storage backed by the OS keychain via ``keyring``."""

    def __init__(
        self,
        service: str = KEYRING_SERVICE,
        username: str | None = None,
        profile: str = "",
    ) -> None:
        self._service = service
        self._username = username or keyring_username(profile)

    def _backend(self):  # type: ignore[no-untyped-def]
        import keyring  # imported lazily so tests never touch the real keychain

        return keyring

    def get_cookie(self) -> str | None:
        try:
            value = self._backend().get_password(self._service, self._username)
        except Exception as exc:  # keyring raises backend-specific errors
            raise SecretStoreError(f"could not read the keyring: {type(exc).__name__}") from exc
        return value or None

    def set_cookie(self, value: str) -> None:
        if not value.strip():
            raise ValueError("cookie value is empty")
        try:
            self._backend().set_password(self._service, self._username, value.strip())
        except Exception as exc:
            raise SecretStoreError(f"could not write the keyring: {type(exc).__name__}") from exc

    def delete_cookie(self) -> None:
        keyring = self._backend()
        try:
            keyring.delete_password(self._service, self._username)
        except keyring.errors.PasswordDeleteError:
            return
        except Exception as exc:
            raise SecretStoreError(f"could not update the keyring: {type(exc).__name__}") from exc


class MemorySecretStore:
    """In-memory store for tests and one-off CLI verification."""

    def __init__(self, cookie: str | None = None) -> None:
        self._cookie = cookie

    def get_cookie(self) -> str | None:
        return self._cookie

    def set_cookie(self, value: str) -> None:
        self._cookie = value

    def delete_cookie(self) -> None:
        self._cookie = None


class Redactor:
    """Replaces known secret values and cookie-shaped text with a placeholder."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: list[str] = []
        for secret in secrets:
            self.add(secret)

    def add(self, secret: str | None) -> None:
        if not secret:
            return
        value = secret.strip()
        if len(value) < 8:  # too short to be a credential; avoid mangling normal text
            return
        if value not in self._secrets:
            self._secrets.append(value)
        # Also redact each cookie pair's value individually, in case only part leaks.
        for pair in value.split(";"):
            name, sep, pair_value = pair.strip().partition("=")
            if name.strip() in NON_SECRET_COOKIE_NAMES:
                continue
            if sep and len(pair_value) >= 8 and pair_value not in self._secrets:
                self._secrets.append(pair_value)

    def redact(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        for pattern in _GENERIC_PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + REDACTED, text)
        return text

    def __call__(self, text: str) -> str:
        return self.redact(text)


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs secrets from the message and its arguments."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redactor.redact(record.msg)
        # Only strings can carry a secret; leave numbers alone so %d formats still work.
        if isinstance(record.args, dict):
            record.args = {k: self._scrub(v) for k, v in record.args.items()}
        elif record.args:
            record.args = tuple(self._scrub(a) for a in record.args)
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            exc.args = tuple(self._redactor.redact(str(a)) for a in exc.args)
        return True

    def _scrub(self, value: object) -> object:
        return self._redactor.redact(value) if isinstance(value, str) else value


_GLOBAL_REDACTOR = Redactor()


def global_redactor() -> Redactor:
    """Process-wide redactor: the CLI and the server register the cookie here."""
    return _GLOBAL_REDACTOR


def install_log_redaction(redactor: Redactor | None = None) -> None:
    """Attach the redacting filter to the root logger and every existing handler."""
    flt = RedactingFilter(redactor or _GLOBAL_REDACTOR)
    root = logging.getLogger()
    root.addFilter(flt)
    for handler in root.handlers:
        handler.addFilter(flt)
    # httpx logs request lines at INFO; they carry no headers, but keep them quiet anyway.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
