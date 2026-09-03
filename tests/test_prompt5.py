"""Prompt 5: poll note placement, history pagination, status session line, sparklines."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from conftest import make_client, make_handler
from quotalens import service
from quotalens.api import create_app
from quotalens.parse import QuotaReading
from quotalens.sessions import SESSION_LENGTH_S, rebuild


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _seed_windows(store, now: int, count: int, peak: float = 40.0) -> None:
    """``count`` complete 5-hour windows back to back, ending before now, plus a current one."""
    for k in range(count):
        end = now - (count - k) * SESSION_LENGTH_S - 3600
        start = end - SESSION_LENGTH_S
        for i in range(0, 300, 5):
            pct = min(peak, i * peak / 240)
            store.record_quota(start + i * 60, [QuotaReading("five_hour", "5-hour", pct, iso(end))])
    cur_end = now + 2 * 3600
    for i in range(0, 180, 5):
        ts = cur_end - SESSION_LENGTH_S + i * 60
        store.record_quota(ts, [QuotaReading("five_hour", "5-hour", i / 3, iso(cur_end))])


def test_poll_note_sits_beside_the_button_not_in_the_hero(settings, store, secrets) -> None:
    app = create_app(
        settings, store, secrets, client_factory=lambda c: make_client(make_handler(), c)
    )
    with TestClient(app) as tc:
        tc.post("/api/poll")
        tc.post("/api/poll")  # suppressed
        html = tc.get("/").text
    assert 'id="poll-note"' in html and "Forced poll suppressed" in html
    assert html.index("Forced poll suppressed") > html.index('id="poll-form"')
    assert "hstrip" not in html


def test_history_paginates_past_twenty_rows(settings, store, secrets) -> None:
    now = int(time.time())
    _seed_windows(store, now, 24)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    with TestClient(app) as tc:
        first = tc.get("/?sort=consumed").text
        everything = tc.get("/?sort=consumed&history=all").text
    assert first.count('class="sess" data-session=') == 20
    assert "show all 25 windows" in first and "history=all" in first
    assert everything.count('class="sess" data-session=') == 25
    assert "show the first 20" in everything


def test_status_prints_the_session_window_and_countdown(tmp_path) -> None:
    now = 1_800_000_000
    dash = {
        "runway": {
            "reset_ts": now + 3900,
            "headroom_pct": 42.0,
            "verdict": "At this rate you finish with 30% unused.",
        },
        "sessions": [{"started_at": now - 14100, "ends_at": now + 3900, "is_current": True}],
    }
    health = {
        "started_ts": now - 60,
        "collector": {"kind": "ok"},
        "poller": {"state": "ok", "last_success_ts": now},
    }

    def fetch(url: str):
        if url.endswith("/api/health"):
            return health
        if url.endswith("/api/dashboard"):
            return dash
        return {"readings": []}

    report = service.status(tmp_path, 8787, fetch=fetch, now=now)
    assert any(
        line.startswith("session: ") and "resets in 1h 05m, 42% headroom" in line
        for line in report.lines
    )
    assert any(line == "verdict: At this rate you finish with 30% unused." for line in report.lines)


def test_median_comparison_appears_after_five_complete_windows(settings, store, secrets) -> None:
    now = int(time.time())
    _seed_windows(store, now, 5, peak=40.0)
    rebuild(store, now)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    with TestClient(app) as tc:
        body = tc.get("/api/dashboard").json()
    assert "your median window (40%)" in body["burn"]["why"]
    store2 = store
    del store2
