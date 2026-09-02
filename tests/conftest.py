"""Shared fixtures. No test may touch the network: every client uses FakeTransport."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import pytest

from quotalens.client import ClaudeClient, RawResponse, TransportError
from quotalens.config import Settings
from quotalens.secrets import MemorySecretStore, Redactor
from quotalens.store import Store

COOKIE = "sessionKey=sk-ant-sid01-SECRETSECRETSECRET-abc; lastActiveOrg=org-1234-5678-abcd"
COOKIE_NO_ORG = "sessionKey=sk-ant-sid01-SECRETSECRETSECRET-abc"
ORG = "org-1234-5678-abcd"

USAGE_DOCUMENTED: dict[str, Any] = {
    "five_hour": {"utilization": 42.0, "resets_at": "2026-09-02T18:00:00+00:00"},
    "seven_day": {"utilization": 17.5, "resets_at": "2026-09-05T09:00:00+00:00"},
    "seven_day_sonnet": {"utilization": 0, "resets_at": None},
    "limits": [
        {
            "percent": 12,
            "resets_at": "2026-09-05T09:00:00+00:00",
            "scope": {"model": {"display_name": "Opus", "id": "claude-opus"}},
        }
    ],
}

# Shape observed live on 2026-09-02 (values changed, structure kept).
USAGE_LIVE_2026_09: dict[str, Any] = {
    "five_hour": {
        "limit_dollars": None,
        "locked_reason": None,
        "remaining_dollars": None,
        "resets_at": "2026-09-02T12:40:00.421772+00:00",
        "used_dollars": None,
        "utilization": 71,
    },
    "seven_day": {"resets_at": "2026-09-07T00:59:59.421799+00:00", "utilization": 38},
    "seven_day_oauth_apps": None,
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "seven_day_cowork": None,
    "tangelo": None,
    "nimbus_quill": {"resets_at": None, "utilization": 0},
    "extra_usage": {
        "credits_ever_enabled": True,
        "currency": "USD",
        "decimal_places": 2,
        "disabled_reason": "org_level_disabled_until",
        "is_enabled": False,
        "monthly_limit": 200,
        "spend_limit_reached": True,
        "used_credits": 316,
        "utilization": 100,
    },
    "limits": [
        {
            "group": "session",
            "is_active": True,
            "kind": "session",
            "percent": 71,
            "resets_at": "2026-09-02T12:40:00.421772+00:00",
            "scope": None,
            "severity": "normal",
        },
        {
            "group": "weekly",
            "is_active": False,
            "kind": "weekly_all",
            "percent": 38,
            "resets_at": "2026-09-07T00:59:59.421799+00:00",
            "scope": None,
            "severity": "normal",
        },
        {
            "group": "weekly",
            "is_active": False,
            "kind": "weekly_scoped",
            "percent": 66,
            "resets_at": "2026-09-07T01:00:00.422121+00:00",
            "scope": {"model": {"display_name": "Sonnet", "id": "claude-sonnet"}, "surface": None},
            "severity": "normal",
        },
    ],
    "spend": {
        "cap": {"credits": {"amount_minor": 200, "exponent": 2}, "money": None},
        "enabled": False,
        "limit": {"amount_minor": 200, "currency": "USD", "exponent": 2},
        "percent": 100,
        "severity": "critical",
        "used": {"amount_minor": 316, "currency": "USD", "exponent": 2},
    },
    "member_dashboard_available": False,
}

OVERAGE_DOCUMENTED = {"used_credits": 1250, "monthly_credit_limit": 5000, "currency": "USD"}


@dataclass
class FakeRequest:
    url: SimpleNamespace  # .path
    headers: dict[str, str]  # lower-case keys


Handler = Callable[[FakeRequest], RawResponse]


class FakeTransport:
    """In-memory transport: a handler maps a request to a RawResponse or raises."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.closed = False

    async def get(self, url: str, headers: Mapping[str, str], timeout_s: float) -> RawResponse:
        request = FakeRequest(
            SimpleNamespace(path=urlparse(url).path), {k.lower(): v for k, v in headers.items()}
        )
        return self._handler(request)

    async def close(self) -> None:
        self.closed = True


def json_response(status: int, body: Any, headers: dict[str, str] | None = None) -> RawResponse:
    merged = {"content-type": "application/json"}
    merged.update({k.lower(): v for k, v in (headers or {}).items()})
    return RawResponse(status, merged, json.dumps(body))


def html_response(status: int, text: str, headers: dict[str, str] | None = None) -> RawResponse:
    merged = {"content-type": "text/html; charset=UTF-8"}
    merged.update({k.lower(): v for k, v in (headers or {}).items()})
    return RawResponse(status, merged, text)


def make_handler(
    usage: Any = USAGE_DOCUMENTED,
    overage: Any = OVERAGE_DOCUMENTED,
    *,
    usage_status: int = 200,
    overage_status: int = 200,
    bootstrap: Any = None,
    extra_headers: dict[str, str] | None = None,
    seen: list[FakeRequest] | None = None,
) -> Handler:
    def handler(request: FakeRequest) -> RawResponse:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path == "/api/bootstrap":
            return json_response(200, bootstrap or {"account": {"lastActiveOrgId": ORG}})
        if path.endswith("/usage"):
            return json_response(usage_status, usage, extra_headers)
        if path.endswith("/overage_spend_limit"):
            return json_response(overage_status, overage)
        return json_response(404, {"error": "not found"})

    return handler


def make_client(handler: Handler, cookie: str = COOKIE, **kwargs: Any) -> ClaudeClient:
    return ClaudeClient(cookie, transport=FakeTransport(handler), **kwargs)


def raise_transport(message: str, *, timed_out: bool = False) -> Handler:
    def handler(request: FakeRequest) -> RawResponse:
        raise TransportError(message, timed_out=timed_out)

    return handler


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=tmp_path / "t.db", poll_enabled=False, poll_interval_s=60)


@pytest.fixture
def store(settings: Settings) -> Store:
    s = Store(settings.db_path)
    yield s
    s.close()


@pytest.fixture
def secrets() -> MemorySecretStore:
    return MemorySecretStore(COOKIE)


@pytest.fixture
def redactor() -> Redactor:
    return Redactor([COOKIE])


# Unused-field guard so ruff does not flag the dataclass helper import.
_ = field
