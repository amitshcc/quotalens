from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from conftest import (
    COOKIE,
    COOKIE_NO_ORG,
    ORG,
    FakeRequest,
    html_response,
    json_response,
    make_client,
    make_handler,
    raise_transport,
)
from quotalens.client import (
    MAX_BODY_BYTES,
    AuthError,
    BlockedError,
    ClientError,
    CurlTransport,
    RateLimitedError,
    RawResponse,
    ShapeError,
    UpstreamError,
    build_headers,
    org_id_from_cookie,
)


def test_org_from_cookie() -> None:
    assert org_id_from_cookie(COOKIE) == ORG
    assert org_id_from_cookie("lastActiveOrg=abc-1; sessionKey=x") == "abc-1"
    assert org_id_from_cookie(COOKIE_NO_ORG) is None
    assert org_id_from_cookie("notlastActiveOrg=abc") is None


def test_headers_match_reference_implementation() -> None:
    headers = build_headers("sessionKey=x")
    assert headers["Cookie"] == "sessionKey=x"
    assert headers["Accept"] == "*/*"
    assert headers["Content-Type"] == "application/json"
    assert headers["Origin"] == "https://claude.ai"
    assert headers["Referer"] == "https://claude.ai/"
    assert "User-Agent" not in headers  # the impersonated browser profile supplies it
    assert build_headers("sessionKey=x", user_agent="UA/1")["User-Agent"] == "UA/1"


def test_usage_uses_org_from_cookie_and_sends_headers() -> None:
    seen: list[FakeRequest] = []
    client = make_client(make_handler(seen=seen))
    data = asyncio.run(client.fetch_usage())
    asyncio.run(client.close())
    assert data["five_hour"]["utilization"] == 42.0
    assert [r.url.path for r in seen] == [f"/api/organizations/{ORG}/usage"]
    assert seen[0].headers["cookie"] == COOKIE
    assert seen[0].headers["origin"] == "https://claude.ai"


def test_custom_user_agent_is_sent() -> None:
    seen: list[FakeRequest] = []
    client = make_client(make_handler(seen=seen), user_agent="TestUA/1.0")
    asyncio.run(client.fetch_usage())
    asyncio.run(client.close())
    assert seen[0].headers["user-agent"] == "TestUA/1.0"


def test_org_falls_back_to_bootstrap_and_is_cached() -> None:
    seen: list[FakeRequest] = []
    client = make_client(make_handler(seen=seen), cookie=COOKIE_NO_ORG)

    async def run() -> None:
        await client.fetch_usage()
        await client.fetch_overage()

    asyncio.run(run())
    asyncio.run(client.close())
    assert [r.url.path for r in seen] == [
        "/api/bootstrap",
        f"/api/organizations/{ORG}/usage",
        f"/api/organizations/{ORG}/overage_spend_limit",
    ]
    assert client.org_id == ORG


def test_bootstrap_membership_fallback() -> None:
    bootstrap = {"account": {"memberships": [{"organization": {"uuid": "org-from-membership"}}]}}
    client = make_client(make_handler(bootstrap=bootstrap), cookie=COOKIE_NO_ORG)
    assert asyncio.run(client.resolve_org()) == "org-from-membership"


def test_bootstrap_without_org_is_shape_error() -> None:
    client = make_client(make_handler(bootstrap={"account": {}}), cookie=COOKIE_NO_ORG)
    with pytest.raises(ShapeError):
        asyncio.run(client.resolve_org())


@pytest.mark.parametrize("status", [401, 403])
def test_401_403_json_is_auth_error(status: int) -> None:
    client = make_client(make_handler(usage_status=status))
    with pytest.raises(AuthError) as exc:
        asyncio.run(client.fetch_usage())
    assert exc.value.status == status


def test_429_carries_retry_after() -> None:
    client = make_client(make_handler(usage_status=429, extra_headers={"Retry-After": "120"}))
    with pytest.raises(RateLimitedError) as exc:
        asyncio.run(client.fetch_usage())
    assert exc.value.retry_after == 120.0


def test_non_json_body_is_shape_error() -> None:
    client = make_client(lambda r: html_response(200, "<html>login</html>"))
    with pytest.raises(ShapeError):
        asyncio.run(client.fetch_usage())


def test_redirect_treated_as_auth_error() -> None:
    client = make_client(lambda r: RawResponse(302, {"location": "/login"}))
    with pytest.raises(AuthError):
        asyncio.run(client.fetch_usage())


def test_timeout_is_upstream_error() -> None:
    client = make_client(raise_transport("Timeout", timed_out=True))
    with pytest.raises(UpstreamError, match="timeout"):
        asyncio.run(client.fetch_usage())


def test_connect_error_is_upstream_error() -> None:
    client = make_client(raise_transport("ConnectionError"))
    with pytest.raises(UpstreamError, match="ConnectionError"):
        asyncio.run(client.fetch_usage())


def test_empty_cookie_rejected() -> None:
    with pytest.raises(ValueError):
        make_client(lambda r: json_response(200, {}), cookie="   ")


def test_cloudflare_challenge_is_blocked_error_not_auth() -> None:
    client = make_client(
        lambda r: html_response(
            403, "<html><title>Just a moment...</title></html>", {"cf-mitigated": "challenge"}
        )
    )
    with pytest.raises(BlockedError, match="Cloudflare"):
        asyncio.run(client.fetch_usage())


def test_cloudflare_html_without_header_is_still_blocked() -> None:
    client = make_client(lambda r: html_response(403, "<title>Just a moment...</title>"))
    with pytest.raises(BlockedError):
        asyncio.run(client.fetch_usage())


def test_json_error_detail_is_included_but_capped() -> None:
    body = {"type": "error", "error": {"type": "permission_error", "message": "x" * 500}}
    client = make_client(make_handler(usage=body, usage_status=403))
    with pytest.raises(AuthError) as exc:
        asyncio.run(client.fetch_usage())
    assert "permission_error" in str(exc.value)
    assert len(str(exc.value)) < 400


def test_close_closes_transport() -> None:
    client = make_client(make_handler())
    asyncio.run(client.close())
    assert client._transport.closed  # type: ignore[attr-defined]


# -- the upstream body is bounded ------------------------------------------------


class _StubSession:
    """Stands in for `curl_cffi`'s AsyncSession, returning a body of a given size."""

    def __init__(self, size: int) -> None:
        self.body = b"x" * size

    async def get(self, url, headers, timeout, allow_redirects):
        return SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            content=self.body,
            text=self.body.decode(),
        )

    async def close(self) -> None:
        pass


def _transport(size: int) -> CurlTransport:
    transport = CurlTransport.__new__(CurlTransport)  # no real session; nothing goes out
    transport._session = _StubSession(size)
    return transport


def test_a_body_one_byte_over_the_cap_is_refused() -> None:
    """There was no cap: `response.text` went whole into the `sample` table, and
    `sample_keep` bounds rows, not bytes. A hostile upstream could fill the disk.
    """
    with pytest.raises(UpstreamError) as caught:
        asyncio.run(_transport(MAX_BODY_BYTES + 1).get("https://x.invalid/u", {}, 5.0))
    assert str(MAX_BODY_BYTES) in str(caught.value)
    assert isinstance(caught.value, ClientError), "poll_once records it as an upstream failure"


def test_a_body_exactly_at_the_cap_is_allowed() -> None:
    """Off by one in the other direction would refuse a payload nothing is wrong with."""
    response = asyncio.run(_transport(MAX_BODY_BYTES).get("https://x.invalid/u", {}, 5.0))
    assert response.status == 200 and len(response.text) == MAX_BODY_BYTES


def test_a_real_sized_payload_is_nowhere_near_it() -> None:
    """Measured 2,028 bytes on the real endpoint; the cap is three orders above."""
    assert asyncio.run(_transport(2_028).get("https://x.invalid/u", {}, 5.0)).status == 200
