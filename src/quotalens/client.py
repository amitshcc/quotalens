"""HTTP client for the internal claude.ai quota endpoints.

These endpoints are undocumented and may change. The client's job is to fetch
raw JSON and translate HTTP failures into a small set of typed errors; parsing
lives in :mod:`quotalens.parse` so drift is handled in one place.

claude.ai is behind Cloudflare bot protection that fingerprints the TLS
handshake. A plain Python client gets a challenge page even with a valid
cookie, so the default transport is ``curl_cffi`` impersonating a browser.
The transport is an interface so tests never touch the network.

No error raised from here ever carries request headers or the cookie.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from quotalens.config import (
    DEFAULT_BASE_URL,
    DEFAULT_HTTP_TIMEOUT_S,
    DEFAULT_IMPERSONATE,
    DEFAULT_USER_AGENT,
)

MAX_ERROR_DETAIL = 160
CURL_TIMEOUT_CODE = 28  # CURLE_OPERATION_TIMEDOUT

_ORG_COOKIE_RE = re.compile(r"(?:^|;\s*)lastActiveOrg=([A-Za-z0-9\-]+)")
_ORG_ID_RE = re.compile(r"^[A-Za-z0-9\-]{8,}$")


# -- transport ------------------------------------------------------------------


@dataclass(frozen=True)
class RawResponse:
    """The little we need from an HTTP response. Header names are lower-case."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    text: str = ""

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def json(self) -> Any:
        return json.loads(self.text)


class TransportError(Exception):
    """Connection-level failure. The message is a type name, never request data."""

    def __init__(self, message: str, *, timed_out: bool = False) -> None:
        super().__init__(message)
        self.timed_out = timed_out


class Transport(Protocol):
    async def get(self, url: str, headers: Mapping[str, str], timeout_s: float) -> RawResponse: ...

    async def close(self) -> None: ...


class CurlTransport:
    """``curl_cffi`` session with browser impersonation; redirects are not followed."""

    def __init__(self, impersonate: str = DEFAULT_IMPERSONATE) -> None:
        from curl_cffi.requests import AsyncSession  # imported lazily: tests never need it

        self._session = AsyncSession(impersonate=impersonate)

    async def get(self, url: str, headers: Mapping[str, str], timeout_s: float) -> RawResponse:
        try:
            response = await self._session.get(
                url, headers=dict(headers), timeout=timeout_s, allow_redirects=False
            )
        except Exception as exc:
            # curl_cffi messages can include the URL but never headers; keep only the type.
            timed_out = type(exc).__name__ == "Timeout" or getattr(exc, "code", None) == (
                CURL_TIMEOUT_CODE
            )
            raise TransportError(type(exc).__name__, timed_out=timed_out) from exc
        headers_out = {str(k).lower(): str(v) for k, v in response.headers.items()}
        return RawResponse(response.status_code, headers_out, response.text)

    async def close(self) -> None:
        await self._session.close()


# -- errors ---------------------------------------------------------------------


class ClientError(RuntimeError):
    """Base class. ``status`` is the HTTP status when one was received."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AuthError(ClientError):
    """401/403: the cookie is missing, expired, or rejected."""


class BlockedError(ClientError):
    """Cloudflare answered instead of claude.ai: the client itself was rejected."""


class RateLimitedError(ClientError):
    """429: back off hard. ``retry_after`` is seconds if the server said."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after


class UpstreamError(ClientError):
    """Any other non-2xx response, or a transport failure."""


class ShapeError(ClientError):
    """A response was 2xx but did not contain what we needed (endpoint drift)."""


# -- helpers --------------------------------------------------------------------


def org_id_from_cookie(cookie: str) -> str | None:
    """Pull the org id out of a ``lastActiveOrg`` cookie pair, if present."""
    match = _ORG_COOKIE_RE.search(cookie)
    return match.group(1) if match else None


def has_session_key(cookie: str) -> bool:
    return "sessionKey=" in cookie


def build_headers(
    cookie: str, base_url: str = DEFAULT_BASE_URL, user_agent: str | None = DEFAULT_USER_AGENT
) -> dict[str, str]:
    """The header set the reference implementation (ClaudeUsageBar) sends.

    The User-Agent is left to the impersonated browser profile unless overridden,
    so it always matches the TLS fingerprint being presented.
    """
    headers = {
        "Cookie": cookie,
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": base_url,
        "Referer": base_url + "/",
    }
    if user_agent:
        headers["User-Agent"] = user_agent
    return headers


def is_cloudflare_challenge(response: RawResponse) -> bool:
    if (response.header("cf-mitigated") or "").lower() == "challenge":
        return True
    content_type = response.header("content-type") or ""
    if response.status in (403, 503) and "text/html" in content_type:
        head = response.text[:2000]
        return "Just a moment" in head or "cf-chl" in head or "challenge-platform" in head
    return False


def _retry_after_seconds(response: RawResponse) -> float | None:
    raw = response.header("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form; not worth parsing, caller uses its default


def _error_detail(response: RawResponse) -> str:
    """A short, value-free description of an error body: JSON error type/message only."""
    try:
        data = response.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    parts: list[str] = []
    if isinstance(err, dict):
        for key in ("type", "message"):
            if isinstance(err.get(key), str):
                parts.append(err[key])
    elif isinstance(err, str):
        parts.append(err)
    elif isinstance(data.get("message"), str):
        parts.append(data["message"])
    detail = "; ".join(parts).strip()
    return f" ({detail[:MAX_ERROR_DETAIL]})" if detail else ""


# -- client ---------------------------------------------------------------------


class ClaudeClient:
    """Async client. One instance per cookie; the org id is cached after first resolve."""

    def __init__(
        self,
        cookie: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        user_agent: str | None = DEFAULT_USER_AGENT,
        impersonate: str = DEFAULT_IMPERSONATE,
        transport: Transport | None = None,
    ) -> None:
        cookie = cookie.strip()
        if not cookie:
            raise ValueError("cookie is empty")
        self._cookie = cookie
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._headers = build_headers(cookie, self._base_url, user_agent)
        self._org_id: str | None = org_id_from_cookie(cookie)
        self._transport: Transport = transport or CurlTransport(impersonate)

    @property
    def org_id(self) -> str | None:
        return self._org_id

    def uses_cookie(self, cookie: str) -> bool:
        return self._cookie == cookie.strip()

    async def close(self) -> None:
        await self._transport.close()

    async def __aenter__(self) -> ClaudeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _get_json(self, path: str) -> Any:
        try:
            response = await self._transport.get(
                self._base_url + path, self._headers, self._timeout_s
            )
        except TransportError as exc:
            if exc.timed_out:
                raise UpstreamError(f"timeout calling {path}") from exc
            raise UpstreamError(f"transport error calling {path}: {exc}") from exc

        status = response.status
        if is_cloudflare_challenge(response):
            raise BlockedError(
                f"blocked by Cloudflare's bot challenge ({status}) on {path}; the request never "
                "reached claude.ai. Try QUOTALENS_IMPERSONATE=safari or chrome, and if it "
                "persists open an issue with `quotalens probe` output",
                status,
            )
        if status in (401, 403):
            detail = _error_detail(response)
            raise AuthError(
                f"claude.ai rejected the session cookie ({status}) on {path}{detail}", status
            )
        if status == 429:
            raise RateLimitedError(f"rate limited (429) on {path}", _retry_after_seconds(response))
        if status in (301, 302, 303, 307, 308):
            # A redirect to a login page is how an expired session often shows up.
            raise AuthError(
                f"redirected ({status}) on {path}; the session is likely expired", status
            )
        if status >= 400:
            raise UpstreamError(f"HTTP {status} on {path}{_error_detail(response)}", status)
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
