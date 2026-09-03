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


def test_sparkline_per_history_row(settings, store, secrets) -> None:
    from quotalens.dashboard import sparkline
    from quotalens.store import QuotaRow

    now = int(time.time())
    _seed_windows(store, now, 2)
    rebuild(store, now)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    with TestClient(app) as tc:
        html = tc.get("/").text
    assert html.count('<svg class="sp"') == 3
    assert 'stroke-width="var(--trace-ghost)"' in html
    rows = [QuotaRow(1000 + i * 450, "five_hour", "5-hour", i * 2.5, "r") for i in range(41)]
    points = sparkline(rows, 1000)
    assert points.startswith("2.0,17.0") and points.endswith("58.0,1.0")
    assert len(points.split()) <= 42
    assert sparkline(rows[:1], 1000) == ""


def test_future_region_is_blank_not_a_gap_and_projection_reaches_reset(
    settings, store, secrets
) -> None:
    now = int(time.time())
    cur_end = now + 2 * 3600 + 1800  # two and a half hours in
    for i in range(0, 150, 2):  # climbing 20 pts/hr: survives to the reset
        ts = cur_end - SESSION_LENGTH_S + i * 60
        store.record_quota(ts, [QuotaReading("five_hour", "5-hour", i / 3, iso(cur_end))])
    rebuild(store, now)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    with TestClient(app) as tc:
        html = tc.get("/").text
        body = tc.get("/api/dashboard").json()
    assert body["range"]["key"] == "session" and body["range"]["end"] == cur_end
    assert body["gap_minutes"] == 0  # two hours of future are not "not collected"
    assert 'fill="url(#gap)"' not in html
    assert 'class="now"' in html and ">now</text>" in html
    assert 'x2="1150.0"' not in html.split('class="gz"')[0]  # gridlines stop at now
    assert html.count('class="hr"') == 4  # hourly separators inside the window
    assert 'class="proj" stroke="var(--s1)"' in html  # 60 pts/hr on 40% left over 2h: survives
    assert body["runway"]["exhaust_ts"] is None
    assert 'id="reset-in"' in html and "resets in" in html
    assert body["runway"]["sustainable"] == round(body["runway"]["headroom_pct"] / 2.5, 2)
    assert (
        'class="hb done"' in html and 'class="hb partial"' in html and 'class="hb future"' in html
    )


def test_projection_turns_critical_when_exhausted_before_reset(settings, store, secrets) -> None:
    now = int(time.time())
    cur_end = now + 3 * 3600
    for i in range(0, 120, 2):  # two hours in at 40 pts/hr: 80% now, 100% in 30 minutes
        ts = cur_end - SESSION_LENGTH_S + i * 60
        store.record_quota(ts, [QuotaReading("five_hour", "5-hour", i * 2 / 3, iso(cur_end))])
    rebuild(store, now)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    with TestClient(app) as tc:
        html = tc.get("/").text
        body = tc.get("/api/dashboard").json()
    assert body["runway"]["exhaust_ts"] is not None
    assert body["burn"]["why"].startswith("Exhausted at ")
    assert 'class="proj" stroke="var(--st-critical)"' in html
    assert 'class="ax cross"' in html and "exhausted " in html
    assert 'dd class="crit-v"' in html
