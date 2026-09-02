from __future__ import annotations

import asyncio

from conftest import (
    COOKIE,
    USAGE_DOCUMENTED,
    USAGE_LIVE_2026_09,
    FakeRequest,
    RawResponse,
    json_response,
    make_client,
    make_handler,
)
from quotawatch.poller import (
    AUTH_RETRY_S,
    MAX_BACKOFF_S,
    RATE_LIMIT_MAX_S,
    RATE_LIMIT_MIN_S,
    Poller,
    Schedule,
)
from quotawatch.secrets import MemorySecretStore, Redactor


def test_schedule_backoff_doubles_and_caps() -> None:
    s = Schedule(60)
    assert [s.on_failure() for _ in range(6)] == [60, 120, 240, 480, 900, 900]
    assert s.on_success() == 60
    assert s.failures == 0


def test_schedule_auth_and_rate_limit() -> None:
    s = Schedule(60)
    assert s.on_auth_error() == AUTH_RETRY_S
    assert s.on_rate_limited(None) == RATE_LIMIT_MIN_S
    assert s.on_rate_limited(None) == RATE_LIMIT_MIN_S * 2
    assert s.on_rate_limited(3600) == 3600  # Retry-After wins when longer
    for _ in range(10):
        assert s.on_rate_limited(None) <= RATE_LIMIT_MAX_S
    assert s.on_failure() <= MAX_BACKOFF_S


def _poller(settings, store, secrets, handler, clock=None):
    return Poller(
        settings,
        store,
        secrets,
        Redactor(),
        client_factory=lambda cookie: make_client(handler, cookie),
        clock=clock or (lambda: 1_000_000.0),
    )


def test_poll_once_success_writes_quota_sample_overage(settings, store, secrets) -> None:
    poller = _poller(settings, store, secrets, make_handler())
    delay = asyncio.run(poller.poll_once())
    assert delay == 60
    assert poller.status.state == "ok"
    assert poller.status.last_success_ts == 1_000_000
    assert poller.status.overage_available is True
    assert store.counts() == {"quota": 3, "sample": 2, "overage": 1, "event": 1}
    assert {r.window for r in store.latest_quota()} == {"five_hour", "seven_day", "limit:opus"}
    assert [e.kind for e in store.recent_events()] == ["unrecognised_block"]
    assert poller.status.ignored_blocks == [{"key": "seven_day_sonnet", "reason": "no resets_at"}]
    assert poller.status.spend is not None and poller.status.spend.source == "overage_endpoint"


def test_poll_401_marks_auth_expired_and_backs_off_gently(settings, store, secrets) -> None:
    poller = _poller(settings, store, secrets, make_handler(usage_status=401))
    delay = asyncio.run(poller.poll_once())
    assert delay == AUTH_RETRY_S
    assert poller.status.state == "auth_expired"
    assert store.counts()["quota"] == 0
    assert [e.kind for e in store.recent_events()] == ["auth_expired"]


def test_poll_429_backs_off_hard(settings, store, secrets) -> None:
    poller = _poller(
        settings,
        store,
        secrets,
        make_handler(usage_status=429, extra_headers={"Retry-After": "900"}),
    )
    assert asyncio.run(poller.poll_once()) == 900
    assert poller.status.state == "rate_limited"


def test_poll_500_then_recovery(settings, store, secrets) -> None:
    calls = {"n": 0}

    def handler(request: FakeRequest) -> RawResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return json_response(500, {"error": "oops"})
        return make_handler()(request)

    poller = _poller(settings, store, secrets, handler)
    assert asyncio.run(poller.poll_once()) == 60
    assert poller.status.state == "error" and poller.status.consecutive_failures == 1
    assert asyncio.run(poller.poll_once()) == 60
    assert poller.status.state == "ok" and poller.status.consecutive_failures == 0
    assert poller.status.polls_ok == 1 and poller.status.polls_failed == 1


def test_unparseable_payload_records_event_and_stores_no_zero(settings, store, secrets) -> None:
    poller = _poller(settings, store, secrets, make_handler(usage={"message": "maintenance"}))
    asyncio.run(poller.poll_once())
    assert poller.status.state == "error"
    assert store.counts()["quota"] == 0
    assert store.counts()["sample"] == 1  # raw payload kept for debugging
    assert [e.kind for e in store.recent_events()] == ["parse_failed"]


def test_drifted_payload_stores_readings_and_flags_drift(settings, store, secrets) -> None:
    drifted = {"data": {"session": {"percent": 33}}}
    poller = _poller(settings, store, secrets, make_handler(usage=drifted))
    asyncio.run(poller.poll_once())
    assert poller.status.state == "ok"
    assert store.counts()["quota"] == 1
    assert [e.kind for e in store.recent_events()] == ["shape_drift"]


def test_overage_failure_does_not_fail_poll(settings, store, secrets) -> None:
    poller = _poller(settings, store, secrets, make_handler(overage_status=404))
    asyncio.run(poller.poll_once())
    asyncio.run(poller.poll_once())
    assert poller.status.state == "ok"
    assert poller.status.overage_available is False
    kinds = [e.kind for e in store.recent_events()]
    assert kinds.count("overage_unavailable") == 1  # only once


def test_no_cookie_idles(settings, store) -> None:
    poller = _poller(settings, store, MemorySecretStore(None), make_handler())
    assert asyncio.run(poller.poll_once()) == AUTH_RETRY_S
    assert poller.status.state == "no_cookie"


def test_cookie_change_rebuilds_client(settings, store) -> None:
    secrets = MemorySecretStore(COOKIE)
    built: list[str] = []

    def factory(cookie: str):
        built.append(cookie)
        return make_client(make_handler(), cookie)

    poller = Poller(settings, store, secrets, Redactor(), client_factory=factory)
    asyncio.run(poller.poll_once())
    asyncio.run(poller.poll_once())
    secrets.set_cookie("sessionKey=sk-ant-NEWNEWNEWNEW; lastActiveOrg=org-1234-5678-abcd")
    asyncio.run(poller.poll_once())
    assert len(built) == 2
    asyncio.run(poller.stop())


def test_run_loop_polls_and_stops(settings, store, secrets) -> None:
    poller = _poller(settings, store, secrets, make_handler(usage=USAGE_DOCUMENTED))

    async def run() -> None:
        poller.start()
        await asyncio.sleep(0.05)
        await poller.stop()

    asyncio.run(run())
    assert poller.status.polls_ok == 1
    assert poller.status.next_poll_ts == 1_000_000 + 60


def test_overage_401_is_auth_expired_and_loop_survives(settings, store, secrets) -> None:
    """A 401 on the optional endpoint must not escape poll_once (it used to kill the task)."""

    def handler(request: FakeRequest) -> RawResponse:
        if request.url.path.endswith("/overage_spend_limit"):
            return json_response(401, {"error": "unauthorized"})
        return make_handler()(request)

    poller = _poller(settings, store, secrets, handler)
    delay = asyncio.run(poller.poll_once())
    assert delay == AUTH_RETRY_S
    assert poller.status.state == "auth_expired"
    assert store.counts()["quota"] == 3  # usage succeeded, so its readings are kept

    async def run() -> None:
        poller.start()
        await asyncio.sleep(0.05)
        assert not poller._task.done()
        await poller.stop()

    asyncio.run(run())


def test_unexpected_store_error_does_not_kill_loop(settings, store, secrets, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store, "record_quota", boom)
    poller = _poller(settings, store, secrets, make_handler())
    assert asyncio.run(poller.poll_once()) == 60
    assert poller.status.state == "error"
    assert "disk on fire" in (poller.status.last_error or "")
    assert [e.kind for e in store.recent_events()] == ["poll_error"]


def test_overage_flapping_is_reported_each_time(settings, store, secrets) -> None:
    calls = {"n": 0}

    def handler(request: FakeRequest) -> RawResponse:
        if request.url.path.endswith("/overage_spend_limit"):
            calls["n"] += 1
            if calls["n"] in (1, 3):
                return json_response(500, {"error": "flaky"})
        return make_handler()(request)

    poller = _poller(settings, store, secrets, handler)
    for _ in range(3):
        asyncio.run(poller.poll_once())
    kinds = [e.kind for e in store.recent_events()]
    assert kinds.count("overage_unavailable") == 2


def test_cloudflare_block_is_reported_as_blocked(settings, store, secrets) -> None:
    def handler(request: FakeRequest) -> RawResponse:
        return RawResponse(403, {"cf-mitigated": "challenge"}, "<html>")

    poller = _poller(settings, store, secrets, handler)
    assert asyncio.run(poller.poll_once()) == AUTH_RETRY_S
    assert poller.status.state == "blocked"
    assert [e.kind for e in store.recent_events()] == ["blocked"]


def test_live_shape_stores_windows_and_spend_fallback(settings, store, secrets) -> None:
    """The 2026-09 payload: overage endpoint 404s, spend comes from the usage payload."""
    poller = _poller(
        settings, store, secrets, make_handler(usage=USAGE_LIVE_2026_09, overage_status=404)
    )
    asyncio.run(poller.poll_once())
    assert poller.status.state == "ok"
    windows = {r.window: r.pct for r in store.latest_quota()}
    assert windows == {"five_hour": 71, "seven_day": 38, "limit:sonnet": 66}
    assert store.latest_overage() == {
        "ts": 1_000_000,
        "spent_minor": 316,
        "cap_minor": 200,
        "currency": "USD",
        "exponent": 2,
    }
    assert poller.status.overage_available is True
    assert poller.status.spend is not None and poller.status.spend.used_text == "$3.16"
    assert poller.status.ignored_blocks == [{"key": "nimbus_quill", "reason": "no resets_at"}]
    assert sorted(e.kind for e in store.recent_events()) == [
        "overage_unavailable",
        "unrecognised_block",
    ]


def test_unrecognised_block_event_fires_once_per_change(settings, store, secrets) -> None:
    poller = _poller(settings, store, secrets, make_handler(usage=USAGE_LIVE_2026_09))
    asyncio.run(poller.poll_once())
    asyncio.run(poller.poll_once())
    kinds = [e.kind for e in store.recent_events()]
    assert kinds.count("unrecognised_block") == 1
