from __future__ import annotations

import asyncio

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
    AuthError,
    BlockedError,
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
