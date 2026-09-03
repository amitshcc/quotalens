"""Threshold alerts: one event per crossing, an events route, and a webhook that
can never stop the collector."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from conftest import COOKIE, make_client, make_handler
from quotalens.alerts import ALERT_KIND, CLEARED_KIND, ThresholdDetector, describe, payload
from quotalens.api import create_app
from quotalens.parse import QuotaReading
from quotalens.poller import Poller
from quotalens.secrets import Redactor

SECRET = "sk-ant-sid01-SECRETSECRETSECRET-abc"


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


# -- the edge, not the level -------------------------------------------------------


def test_one_event_per_crossing_with_hysteresis() -> None:
    detector = ThresholdDetector(threshold=20.0)
    assert detector.update(5.0) is None
    assert detector.update(25.0) == ALERT_KIND
    assert detector.update(30.0) is None  # still over: not an event again
    assert detector.update(21.0) is None
    assert detector.update(19.0) is None  # inside the hysteresis band, no chatter
    assert detector.update(17.9) == CLEARED_KIND
    assert detector.update(17.0) is None
    assert detector.update(20.0) == ALERT_KIND  # and it can fire again


def test_exactly_on_the_threshold_fires_and_a_missing_rate_does_nothing() -> None:
    detector = ThresholdDetector(threshold=20.0)
    assert detector.update(None) is None
    assert detector.update(20.0) == ALERT_KIND
    assert detector.update(None) is None  # a gap in readings is not a clearing
    assert detector.firing is True


def test_the_sentence_says_what_happened() -> None:
    assert describe(ALERT_KIND, 42.5, 20.0, 37.0) == (
        "Burn rate 42.5 pts/hr crossed the 20 pts/hr threshold, 37% of the session left."
    )
    assert describe(CLEARED_KIND, 3.0, 20.0, None).startswith("Burn rate fell back to 3.0")


# -- the webhook body --------------------------------------------------------------


def test_the_webhook_body_carries_no_account_identifier() -> None:
    body = payload(ALERT_KIND, 1700, 42.0, 20.0, 37.0, "2026-09-03T18:00:00+00:00", "http://x/")
    text = json.dumps(body)
    assert SECRET not in text and COOKIE not in text
    assert "org" not in text.lower() and "uuid" not in text.lower()
    assert set(body) == {
        "event",
        "ts",
        "rate_pts_per_hour",
        "threshold_pts_per_hour",
        "headroom_pct",
        "session_resets_at",
        "text",
        "url",
    }
    assert body["rate_pts_per_hour"] == 42.0 and body["url"] == "http://x/"


# -- through the poller ------------------------------------------------------------


def _climbing(store, now: int, per_minute: float, minutes: int = 20) -> str:
    """Seed a climb, and return a handler payload that continues it consistently."""
    reset = iso(now + 3 * 3600)
    for i in range(minutes):
        ts = now - (minutes - 1 - i) * 60
        store.record_quota(ts, [QuotaReading("five_hour", "5-hour", i * per_minute, reset)])
    return reset


def _continuing(reset: str, pct: float):
    """A fake upstream whose reading belongs to the same window as the seeded ones."""
    return make_handler(
        usage={
            "five_hour": {"utilization": pct, "resets_at": reset},
            "seven_day": {"utilization": 30, "resets_at": reset},
        }
    )


def test_a_crossing_writes_one_event_however_many_polls(settings, store, secrets) -> None:
    now = int(time.time())
    reset = _climbing(store, now, per_minute=1.0)  # 60 pts/hr, over the 20 pts/hr default
    handler = _continuing(reset, 19.0)
    poller = Poller(
        settings,
        store,
        secrets,
        Redactor(),
        client_factory=lambda c: make_client(handler, c),
        clock=lambda: float(now),
    )
    for _ in range(4):
        asyncio.run(poller.poll_once())
    alerts = [e for e in store.recent_events(limit=50) if e.kind == ALERT_KIND]
    assert len(alerts) == 1  # one crossing, four polls
    assert "crossed the 20 pts/hr threshold" in alerts[0].detail
    assert poller.status.burn_rate is not None and poller.status.burn_rate > 20


def test_a_flat_series_never_fires(settings, store, secrets) -> None:
    now = int(time.time())
    reset = _climbing(store, now, per_minute=0.0)
    poller = Poller(
        settings,
        store,
        secrets,
        Redactor(),
        client_factory=lambda c: make_client(_continuing(reset, 0.0), c),
        clock=lambda: float(now),
    )
    asyncio.run(poller.poll_once())
    assert [e for e in store.recent_events(limit=50) if e.kind == ALERT_KIND] == []


def test_the_webhook_fires_once_and_a_failing_one_is_dropped(settings, store, secrets) -> None:
    now = int(time.time())
    reset = _climbing(store, now, per_minute=1.0)
    posts: list[tuple[str, dict]] = []

    async def fake_post(url, body, timeout_s=5.0):
        posts.append((url, body))
        raise RuntimeError("the receiver is down")  # and the poll must not care

    settings = settings.with_overrides(webhook_url="https://example.invalid/hook")
    poller = Poller(
        settings,
        store,
        secrets,
        Redactor(),
        client_factory=lambda c: make_client(_continuing(reset, 19.0), c),
        clock=lambda: float(now),
    )

    async def run() -> None:
        import quotalens.poller as poller_module

        original = poller_module.post_webhook
        poller_module.post_webhook = fake_post
        try:
            for _ in range(3):
                await poller.poll_once()
            await asyncio.sleep(0)  # let the fire-and-forget task run
            await asyncio.sleep(0)
        finally:
            poller_module.post_webhook = original

    asyncio.run(run())
    assert len(posts) == 1  # one crossing, one POST, no retry storm
    url, body = posts[0]
    assert url == "https://example.invalid/hook" and body["event"] == ALERT_KIND
    assert poller.status.state == "ok"  # the failing webhook cost nothing
    assert poller.status.polls_ok == 3


def test_no_webhook_configured_means_no_outbound_request(settings, store, secrets) -> None:
    now = int(time.time())
    reset = _climbing(store, now, per_minute=1.0)
    calls: list[str] = []

    async def fake_post(url, body, timeout_s=5.0):
        calls.append(url)
        return True

    poller = Poller(
        settings,
        store,
        secrets,
        Redactor(),
        client_factory=lambda c: make_client(_continuing(reset, 19.0), c),
        clock=lambda: float(now),
    )

    async def run() -> None:
        import quotalens.poller as poller_module

        original = poller_module.post_webhook
        poller_module.post_webhook = fake_post
        try:
            await poller.poll_once()
            await asyncio.sleep(0)
        finally:
            poller_module.post_webhook = original

    asyncio.run(run())
    assert calls == []  # opt-in means opt-in
    assert [e.kind for e in store.recent_events(limit=50) if e.kind == ALERT_KIND] == [ALERT_KIND]


# -- surfaced --------------------------------------------------------------------


def test_events_route_and_dashboard_surface_the_alert(settings, store, secrets) -> None:
    now = int(time.time())
    _climbing(store, now, per_minute=1.0)
    store.record_event(ALERT_KIND, "Burn rate 60.0 pts/hr crossed the 20 pts/hr threshold.", ts=now)
    store.record_event("poll_error", "something else entirely", ts=now - 5)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    with TestClient(app) as tc:
        body = tc.get("/api/events").json()
        only = tc.get(f"/api/events?kind={ALERT_KIND}&limit=5").json()
        html = tc.get("/").text
        view = tc.get("/api/dashboard").json()
    assert body["events"][0]["kind"] == ALERT_KIND
    assert [e["kind"] for e in only["events"]] == [ALERT_KIND]
    assert "Recent events" in html and "crossed the 20 pts/hr threshold" in html
    assert "burn alert" in html and "ev-alert" in html
    assert view["alert_standing"] is True


def test_a_cleared_alert_stops_standing(settings, store, secrets) -> None:
    now = int(time.time())
    store.record_event(ALERT_KIND, "crossed", ts=now - 60)
    store.record_event(CLEARED_KIND, "fell back", ts=now)
    app = create_app(settings, store, secrets)
    with TestClient(app) as tc:
        view = tc.get("/api/dashboard").json()
        html = tc.get("/").text
    assert view["alert_standing"] is False
    assert "burn alert" not in html
