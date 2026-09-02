from __future__ import annotations

import asyncio

import httpx
import pytest

from conftest import COOKIE, COOKIE_NO_ORG, ORG, json_response, make_client, make_handler
from quotawatch.client import (
    RateLimitedError,
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
    assert headers["User-Agent"].startswith("Mozilla/5.0")


def test_usage_uses_org_from_cookie_and_sends_headers() -> None:
    seen: list[httpx.Request] = []
    client = make_client(make_handler(seen=seen))
    data = asyncio.run(client.fetch_usage())
    asyncio.run(client.close())
    assert data["five_hour"]["utilization"] == 42.0
    assert [r.url.path for r in seen] == [f"/api/organizations/{ORG}/usage"]
    assert seen[0].headers["cookie"] == COOKIE
    assert seen[0].headers["origin"] == "https://claude.ai"


def test_org_falls_back_to_bootstrap_and_is_cached() -> None:
    seen: list[httpx.Request] = []
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
    asyncio.run(client.close())


def test_bootstrap_without_org_is_shape_error() -> None:
    client = make_client(make_handler(bootstrap={"account": {}}), cookie=COOKIE_NO_ORG)
    with pytest.raises(ShapeError):
        asyncio.run(client.resolve_org())
    asyncio.run(client.close())


def test_429_carries_retry_after() -> None:
    client = make_client(make_handler(usage_status=429, extra_headers={"Retry-After": "120"}))
    with pytest.raises(RateLimitedError) as exc:
        asyncio.run(client.fetch_usage())
    assert exc.value.retry_after == 120.0
    asyncio.run(client.close())


def test_non_json_body_is_shape_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>login</html>")

    client = make_client(handler)
    with pytest.raises(ShapeError):
        asyncio.run(client.fetch_usage())
    asyncio.run(client.close())


def test_redirect_treated_as_auth_error() -> None:
    from quotawatch.client import AuthError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/login"})

    client = make_client(handler)
    with pytest.raises(AuthError):
        asyncio.run(client.fetch_usage())
    asyncio.run(client.close())


def test_timeout_is_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client = make_client(handler)
    with pytest.raises(UpstreamError, match="timeout"):
        asyncio.run(client.fetch_usage())
    asyncio.run(client.close())


def test_empty_cookie_rejected() -> None:
    with pytest.raises(ValueError):
        make_client(lambda r: json_response(200, {}), cookie="   ")
