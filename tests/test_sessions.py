"""Session windows derived from five_hour.resets_at."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from quotalens.api import create_app
from quotalens.dashboard import build_dashboard
from quotalens.parse import QuotaReading
from quotalens.poller import PollerStatus
from quotalens.render import render_app
from quotalens.sessions import SESSION_LENGTH_S, derive_sessions, idle_spans, rebuild
from quotalens.store import QuotaRow, Store
from quotalens.views import ViewOptions

T0 = 1_800_000_000  # a round epoch


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def rate_rows(samples: list[tuple[int, float, int | None]]) -> list[QuotaRow]:
    """(ts, pct, ends_at) -> five_hour rows."""
    return [
        QuotaRow(ts, "five_hour", "5-hour", pct, iso(ends) if ends else None)
        for ts, pct, ends in samples
    ]


def weekly_rows(samples: list[tuple[int, float, str]]) -> list[QuotaRow]:
    return [QuotaRow(ts, "seven_day", "7-day", pct, reset) for ts, pct, reset in samples]


def test_two_consecutive_windows_without_idle_between() -> None:
    e1 = T0 + SESSION_LENGTH_S
    e2 = e1 + SESSION_LENGTH_S
    rows = rate_rows([(T0 + i * 60, i, e1) for i in range(0, 300, 5)])
    rows += rate_rows([(e1 + i * 60, i * 2, e2) for i in range(0, 300, 5)])
    windows = derive_sessions({"five_hour": rows}, now=e2 - 60)
    assert [(w.started_at, w.ends_at) for w in windows] == [(T0, e1), (e1, e2)]
    assert [w.is_current for w in windows] == [False, True]
    assert idle_spans(windows, now=e2 - 60) == []
    assert windows[0].samples == 60 and windows[1].peak_pct == 590


def test_idle_gap_when_no_window_was_active() -> None:
    e1 = T0 + SESSION_LENGTH_S
    # samples keep reporting the expired window for two hours, then a new one starts
    rows = rate_rows([(T0 + i * 60, 40, e1) for i in range(0, 300, 10)])
    rows += rate_rows([(e1 + i * 60, 40, e1) for i in range(2, 120, 10)])  # expired, reported
    start2 = e1 + 2 * 3600
    e2 = start2 + SESSION_LENGTH_S
    rows += rate_rows([(start2 + i * 60, 3 + i, e2) for i in range(0, 60, 10)])
    now = start2 + 3600
    windows = derive_sessions({"five_hour": rows}, now)
    assert [(w.started_at, w.ends_at) for w in windows] == [(T0, e1), (start2, e2)]
    assert windows[0].samples == 30  # post-expiry samples are not part of the window
    assert idle_spans(windows, now) == [(e1, start2)]


def test_window_still_open_at_end_of_data_and_null_reset_ignored() -> None:
    e1 = T0 + SESSION_LENGTH_S
    rows = rate_rows([(T0 - 600, 0, None), (T0, 1, e1), (T0 + 60, 2, e1)])
    windows = derive_sessions({"five_hour": rows}, now=T0 + 120)
    assert len(windows) == 1 and windows[0].is_current
    assert windows[0].samples == 2
    later = derive_sessions({"five_hour": rows}, now=e1 + 1)
    assert not later[0].is_current
    assert idle_spans(later, now=e1 + 3600) == [(e1, e1 + 3600)]


def test_reset_jitter_of_a_second_is_the_same_window() -> None:
    e1 = T0 + SESSION_LENGTH_S
    rows = rate_rows([(T0, 5, e1), (T0 + 60, 6, e1 - 1), (T0 + 120, 7, e1 + 1)])
    windows = derive_sessions({"five_hour": rows}, now=T0 + 200)
    assert len(windows) == 1 and windows[0].samples == 3


def test_peak_versus_final() -> None:
    e1 = T0 + SESSION_LENGTH_S
    rows = rate_rows([(T0, 10, e1), (T0 + 60, 50, e1), (T0 + 120, 30, e1)])
    w = derive_sessions({"five_hour": rows}, now=e1 + 1)[0]
    assert (w.peak_pct, w.final_pct) == (50, 30)


def test_weekly_delta_and_reset_mid_window() -> None:
    e1 = T0 + SESSION_LENGTH_S
    rate = rate_rows([(T0 + i * 60, i, e1) for i in range(0, 200, 20)])
    weekly = weekly_rows([(T0, 38, "w1"), (T0 + 60, 41, "w1"), (T0 + 180, 41, "w1")])
    w = derive_sessions({"five_hour": rate, "seven_day": weekly}, now=e1 + 1)[0]
    assert w.deltas["seven_day"].start == 38 and w.deltas["seven_day"].end == 41
    assert not w.deltas["seven_day"].reset
    # the weekly limit resets inside the window: flagged, not presented as consumption
    weekly = weekly_rows(
        [(T0, 90, "w1"), (T0 + 60, 95, "w1"), (T0 + 120, 2, "w2"), (T0 + 180, 4, "w2")]
    )
    w = derive_sessions({"five_hour": rate, "seven_day": weekly}, now=e1 + 1)[0]
    d = w.deltas["seven_day"]
    assert (d.start, d.end, d.reset) == (90, 4, True)
    assert "limit:fable" not in w.deltas


def test_backfill_is_idempotent_and_survives_reopen(tmp_path) -> None:
    store = Store(tmp_path / "s.db")
    e1 = T0 + SESSION_LENGTH_S
    e2 = e1 + 7200 + SESSION_LENGTH_S
    for i in range(0, 300, 5):
        store.record_quota(T0 + i * 60, [QuotaReading("five_hour", "5-hour", i, iso(e1))])
    for i in range(0, 60, 5):
        store.record_quota(e1 + 7200 + i * 60, [QuotaReading("five_hour", "5-hour", i, iso(e2))])
    assert rebuild(store, now=e1 + 7200 + 3600) == 2
    assert rebuild(store, now=e1 + 7200 + 3600) == 2
    assert store.counts()["session_window"] == 2
    rows = store.sessions(order="consumed")
    assert rows[0]["peak_pct"] == 295 and rows[0]["started_at"] == T0
    store.close()
    store = Store(tmp_path / "s.db")
    assert store.counts()["session_window"] == 2
    store.close()


# -- dashboard integration -------------------------------------------------------


def _seed_sessions(store, now: int) -> tuple[int, int]:
    """Two closed windows and a current one; the middle one consumed the most."""
    e1 = now - 9 * 3600
    e2 = now - 3 * 3600
    e3 = now + 2 * 3600
    for k, (e, peak) in enumerate(((e1, 30), (e2, 80), (e3, 12))):
        start = e - SESSION_LENGTH_S
        for i in range(0, 300, 2):
            ts = start + i * 60
            if ts > now:
                break
            store.record_quota(
                ts,
                [
                    QuotaReading("five_hour", "5-hour", min(peak, i * peak / 100), iso(e)),
                    QuotaReading("seven_day", "7-day", 40 + k * 5 + i / 60, "wk"),
                    QuotaReading("limit:fable", "Fable", 70 + k + i / 90, "wf"),
                ],
            )
    return e2 - SESSION_LENGTH_S, e2


def test_history_table_sorted_and_row_links_select_the_window(settings, store, secrets) -> None:
    now = int(time.time())
    start2, end2 = _seed_sessions(store, now)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    with TestClient(app) as tc:
        recent = tc.get("/").text
        by_use = tc.get("/?sort=consumed").text
        selected = tc.get(f"/?range={start2}-{end2}&sort=consumed").text
        json_view = tc.get("/api/dashboard?sort=consumed").json()
    assert "History — 5-hour session windows, most recent first" in recent
    assert '<th class="n">All models</th>' in recent and '<th class="n">Fable</th>' in recent
    assert "limit:fable" not in recent.split('id="chart-data"')[0]
    assert json_view["sessions"][0]["peak"] == "80%"  # the expensive window first
    assert f'href="/?range={start2}-{end2}&amp;sort=consumed"' in by_use
    assert "r-on" in selected and "r-on" not in by_use  # the chosen window is highlighted
    assert "current</span>" in recent
    assert "% → " in recent  # weekly start -> end figures


def test_thin_windows_render_far_and_idle_differs_from_gap(settings, store) -> None:
    now = int(time.time())
    e1 = now - 6 * 3600
    for i in range(3):  # a window with only three samples
        row = QuotaReading("five_hour", "5-hour", 5 + i, iso(e1))
        store.record_quota(e1 - 3600 + i * 60, [row])
    cur_end = now + 3600
    for i in range(0, 240, 2):  # the current window, sampled every two minutes from its start
        row = QuotaReading("five_hour", "5-hour", i / 4, iso(cur_end))
        store.record_quota(cur_end - SESSION_LENGTH_S + i * 60, [row])
    rebuild(store, now)
    status = PollerStatus(state="ok", last_success_ts=now)
    dash = build_dashboard(settings, store, status, now, 20.0, ViewOptions(range_key="24h"))
    thin = [r for r in dash.history.rows if r.samples == 3]
    assert thin and thin[0].thin and not dash.history.rows[0].thin
    assert dash.chart.idle_minutes > 0 and dash.chart.gap_minutes > 0
    assert len(dash.chart.session_x) == 2
    html = render_app(dash)
    assert 'class="idle"' in html and 'fill="url(#gap)"' in html and 'class="sess"' in html
    assert 'class="r-thin"' in html
