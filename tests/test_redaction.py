"""The cookie must never appear in logs or in an HTTP error body."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from conftest import COOKIE, make_client, make_handler, raise_transport
from quotalens.api import create_app
from quotalens.secrets import (
    REDACTED,
    RedactingFilter,
    Redactor,
    install_log_redaction,
)

SECRET = "sk-ant-sid01-SECRETSECRETSECRET-abc"


def test_redactor_replaces_known_value_and_pairs() -> None:
    r = Redactor([COOKIE])
    assert SECRET not in r.redact(f"header was {COOKIE}")
    assert SECRET not in r.redact(f"only the token: {SECRET}")
    # The org id is an identifier, not a credential; it stays visible for debugging.
    assert "org-1234-5678-abcd" in r.redact("403 on /api/organizations/org-1234-5678-abcd/usage")


def test_redactor_generic_patterns_without_known_value() -> None:
    r = Redactor()
    assert r.redact("Cookie: sessionKey=abcdefgh123; other=1") == f"Cookie: {REDACTED}"
    assert r.redact("sessionKey=zzzzzzzzzz;x") == f"sessionKey={REDACTED};x"
    assert r.redact("token sk-ant-api03-QQQQQQQQ here") == f"token {REDACTED} here"


def test_short_values_are_not_added() -> None:
    r = Redactor(["abc"])
    assert r.redact("abc is fine") == "abc is fine"


def test_logging_filter_scrubs_message_args_and_exceptions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("quotalens.test")
    logger.addFilter(RedactingFilter(Redactor([COOKIE])))
    with caplog.at_level(logging.DEBUG, logger="quotalens.test"):
        logger.info("cookie is %s", COOKIE)
        logger.info(f"inline {COOKIE}")
        try:
            raise RuntimeError(f"failed with Cookie: {COOKIE}")
        except RuntimeError:
            logger.exception("boom")
    assert SECRET not in caplog.text
    assert "cookie" in caplog.text.lower()
    # Every log line that mentions a cookie ends with the placeholder, never a value.
    assert "cookie is [REDACTED]" in caplog.text
    assert "inline [REDACTED]" in caplog.text
    assert "failed with Cookie: [REDACTED]" in caplog.text


def test_logging_filter_keeps_numeric_args_formattable(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("quotalens.numeric")
    logger.addFilter(RedactingFilter(Redactor([COOKIE])))
    with caplog.at_level(logging.INFO, logger="quotalens.numeric"):
        logger.info("poll ok: %d readings in %.1fs for %s", 3, 0.25, COOKIE)
    assert "poll ok: 3 readings in 0.2s for [REDACTED]" in caplog.text


def test_install_log_redaction_covers_root(caplog: pytest.LogCaptureFixture) -> None:
    redactor = Redactor([COOKIE])
    install_log_redaction(redactor)
    try:
        with caplog.at_level(logging.INFO):
            logging.getLogger("anything").warning("leak? %s", COOKIE)
        assert SECRET not in caplog.text
    finally:
        root = logging.getLogger()
        for flt in list(root.filters):
            root.removeFilter(flt)


def test_client_errors_never_carry_headers_or_cookie() -> None:
    import asyncio

    from quotalens.client import AuthError, ClientError, RateLimitedError

    async def run() -> list[str]:
        messages = []
        for status in (401, 403, 429, 500):
            client = make_client(make_handler(usage_status=status))
            with pytest.raises(ClientError) as exc:
                await client.fetch_usage()
            messages.append(repr(exc.value) + str(exc.value))
            if status in (401, 403):
                assert isinstance(exc.value, AuthError)
            if status == 429:
                assert isinstance(exc.value, RateLimitedError)
            await client.close()
        return messages

    for message in asyncio.run(run()):
        assert SECRET not in message
        assert "Cookie" not in message


def test_api_error_response_body_never_contains_cookie(settings, store, secrets) -> None:
    redactor = Redactor([COOKIE])
    app = create_app(settings, store, secrets, redactor=redactor)

    def explode() -> list:
        raise RuntimeError(f"request failed, Cookie: {COOKIE}")

    store.latest_quota = explode  # type: ignore[method-assign]
    with TestClient(app, raise_server_exceptions=False) as tc:
        response = tc.get("/api/quota/current")
    assert response.status_code == 500
    assert SECRET not in response.text
    assert "REDACTED" not in response.text  # body is generic, not a scrubbed traceback
    assert response.json()["error"]


def test_health_error_string_is_redacted(settings, store, secrets) -> None:
    """A poll failure whose message contains the cookie shows up scrubbed on /api/health."""
    import asyncio

    from quotalens.poller import Poller

    def factory(cookie: str):
        return make_client(raise_transport(f"refused; Cookie: {cookie}"), cookie)

    redactor = Redactor()
    poller = Poller(settings, store, secrets, redactor, client_factory=factory)
    asyncio.run(poller.poll_once())
    asyncio.run(poller.stop())
    assert poller.status.state == "error"
    assert SECRET not in (poller.status.last_error or "")
    for event in store.recent_events():
        assert SECRET not in event.detail
