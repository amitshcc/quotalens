"""Shared fixtures. No test may touch the network: every client uses httpx.MockTransport."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from quotawatch.client import ClaudeClient
from quotawatch.config import Settings
from quotawatch.secrets import MemorySecretStore, Redactor
from quotawatch.store import Store

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

OVERAGE_DOCUMENTED = {"used_credits": 1250, "monthly_credit_limit": 5000, "currency": "USD"}

Handler = Callable[[httpx.Request], httpx.Response]


def json_response(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers=headers)


def make_handler(
    usage: Any = USAGE_DOCUMENTED,
    overage: Any = OVERAGE_DOCUMENTED,
    *,
    usage_status: int = 200,
    overage_status: int = 200,
    bootstrap: Any = None,
    extra_headers: dict[str, str] | None = None,
    seen: list[httpx.Request] | None = None,
) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
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


def make_client(handler: Handler, cookie: str = COOKIE) -> ClaudeClient:
    return ClaudeClient(cookie, transport=httpx.MockTransport(handler))


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
