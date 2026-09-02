"""HTTP client for the internal claude.ai quota endpoints.

These endpoints are undocumented and may change. The client's job is to fetch
raw JSON and translate HTTP failures into a small set of typed errors; parsing
lives in :mod:`quotawatch.parse` so drift is handled in one place.

No error raised from here ever carries request headers or the cookie.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from quotawatch.config import DEFAULT_BASE_URL, DEFAULT_HTTP_TIMEOUT_S

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_ORG_COOKIE_RE = re.compile(r"(?:^|;\s*)lastActiveOrg=([A-Za-z0-9\-]+)")
_ORG_ID_RE = re.compile(r"^[A-Za-z0-9\-]{8,}$")


class ClientError(RuntimeError):
    """Base class. ``status`` is the HTTP status when one was received."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AuthError(ClientError):
    """401/403: the cookie is missing, expired, or rejected."""


class RateLimitedError(ClientError):
    """429: back off hard. ``retry_after`` is seconds if the server said."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after


class UpstreamError(ClientError):
    """Any other non-2xx response, or a transport failure."""


class ShapeError(ClientError):
    """A response was 2xx but did not contain what we needed (endpoint drift)."""


def org_id_from_cookie(cookie: str) -> str | None:
    """Pull the org id out of a ``lastActiveOrg`` cookie pair, if present."""
    match = _ORG_COOKIE_RE.search(cookie)
    return match.group(1) if match else None


def has_session_key(cookie: str) -> bool:
    return "sessionKey=" in cookie


def build_headers(cookie: str, base_url: str = DEFAULT_BASE_URL) -> dict[str, str]:
    """The header set the reference implementation (ClaudeUsageBar) sends."""
    return {
        "Cookie": cookie,
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": base_url,
        "Referer": base_url + "/",
        "User-Agent": USER_AGENT,
    }


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form; not worth parsing, caller uses its default


class ClaudeClient:
    """Async client. One instance per cookie; the org id is cached after first resolve."""

    def __init__(
        self,
        cookie: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        cookie = cookie.strip()
        if not cookie:
            raise ValueError("cookie is empty")
        self._cookie = cookie
        self._base_url = base_url.rstrip("/")
        self._org_id: str | None = org_id_from_cookie(cookie)
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers=build_headers(cookie, self._base_url),
            timeout=timeout_s,
            transport=transport,
            follow_redirects=False,
        )

    @property
    def org_id(self) -> str | None:
        return self._org_id

    def uses_cookie(self, cookie: str) -> bool:
        return self._cookie == cookie.strip()

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> ClaudeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _get_json(self, path: str) -> Any:
        try:
            response = await self._http.get(path)
        except httpx.TimeoutException as exc:
            raise UpstreamError(f"timeout calling {path}") from exc
        except httpx.HTTPError as exc:
            # str(exc) on transport errors never includes headers, but keep it terse anyway.
            raise UpstreamError(f"transport error calling {path}: {type(exc).__name__}") from exc

        status = response.status_code
        if status in (401, 403):
            raise AuthError(f"claude.ai rejected the session cookie ({status}) on {path}", status)
        if status == 429:
            raise RateLimitedError(f"rate limited (429) on {path}", _retry_after_seconds(response))
        if status in (301, 302, 303, 307, 308):
            # A redirect to a login page is how an expired session often shows up.
            raise AuthError(
                f"redirected ({status}) on {path}; the session is likely expired", status
            )
        if status >= 400:
            raise UpstreamError(f"HTTP {status} on {path}", status)
        try:
            return response.json()
        except ValueError as exc:
            raise ShapeError(f"non-JSON body ({status}) on {path}", status) from exc

    async def fetch_bootstrap(self) -> Any:
        return await self._get_json("/api/bootstrap")

    async def resolve_org(self) -> str:
        """Org id from the cookie, else from ``/api/bootstrap``. Cached."""
        if self._org_id:
            return self._org_id
        data = await self.fetch_bootstrap()
        org_id = _org_from_bootstrap(data)
        if not org_id:
            raise ShapeError("/api/bootstrap did not contain account.lastActiveOrgId")
        self._org_id = org_id
        return org_id

    async def fetch_usage(self) -> Any:
        org = await self.resolve_org()
        return await self._get_json(f"/api/organizations/{org}/usage")

    async def fetch_overage(self) -> Any:
        org = await self.resolve_org()
        return await self._get_json(f"/api/organizations/{org}/overage_spend_limit")


def _org_from_bootstrap(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    account = data.get("account")
    candidates: list[Any] = []
    if isinstance(account, dict):
        candidates.append(account.get("lastActiveOrgId"))
        memberships = account.get("memberships")
        if isinstance(memberships, list):
            for membership in memberships:
                org = membership.get("organization") if isinstance(membership, dict) else None
                if isinstance(org, dict):
                    candidates.append(org.get("uuid"))
    for candidate in candidates:
        if isinstance(candidate, str) and _ORG_ID_RE.match(candidate):
            return candidate
    return None
