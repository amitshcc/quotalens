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
from quotalens.sessions import (
    SESSION_LENGTH_S,
    coverage_pct,
    derive_sessions,
    idle_spans,
    rebuild,
    rebuild_recent,
    reset_model_violation,
)
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


def test_coverage_is_observed_time_not_the_viewer_interval() -> None:
    from quotalens.sessions import observed_seconds

    assert coverage_pct(17_230, 5 * 3600) == 95.7
    assert coverage_pct(18_000, 5 * 3600) == 100.0
    assert coverage_pct(20_000, 5 * 3600) == 100.0  # capped
    assert coverage_pct(30, 30) == 100.0  # a window shorter than one interval, fully seen
    assert coverage_pct(0, 30) == 0.0
    # samples every 30s and every 60s over the same hour both count as fully observed
    hour = list(range(0, 3601, 30))
    assert observed_seconds(hour, 0, 3600, 180) == 3600
    assert observed_seconds(hour[::2], 0, 3600, 180) == 3600
    # a 20-minute silence in the middle is not observed; edges count up to the threshold
    gappy = [t for t in hour if not 1200 < t < 2400]
    assert observed_seconds(gappy, 0, 3600, 180) == 3600 - (2400 - 1200)
    assert observed_seconds([1000, 1060], 0, 3600, 180) == 60 + 180 + 180
    assert observed_seconds([], 0, 3600, 180) == 0


def test_interleaved_expiries_merge_into_one_window_each() -> None:
    """Two collectors writing alternately: their samples merge by expiry, no duplicate starts."""
    e1 = T0 + SESSION_LENGTH_S
    e2 = e1 - 4 * 60  # a second, slightly different expiry
    rows = []
    for i in range(0, 20):
        rows.append(QuotaRow(T0 + i * 60, "five_hour", "5-hour", 10 + i, iso(e1)))
        rows.append(QuotaRow(T0 + i * 60 + 5, "five_hour", "5-hour", 50 + i, iso(e2)))
    windows = derive_sessions({"five_hour": rows}, now=T0 + 3600)
    assert len({w.started_at for w in windows}) == len(windows) == 2
    assert sorted(w.samples for w in windows) == [20, 20]
    # a stray group that appeared later but stopped sampling earlier is not the current window
    rows.append(QuotaRow(T0 + 30 * 60, "five_hour", "5-hour", 40, iso(e1)))
    windows = derive_sessions({"five_hour": rows}, now=T0 + 3600)
    assert [w.is_current for w in sorted(windows, key=lambda w: w.ends_at)] == [False, True]


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
    assert "History — session windows, most recent first" in recent
    assert '<th class="n">Weekly all</th>' in recent and '<th class="n">Weekly Fable</th>' in recent
    assert ">Coverage<" not in recent and ">Final<" not in recent
    assert 'title="Peak ' not in recent  # peak and close agree within 2 points
    assert "limit:fable" not in recent.split('id="chart-data"')[0]
    assert json_view["sessions"][0]["peak"] == "80%"  # the expensive window first
    assert f'href="/?range={start2}-{end2}&amp;sort=consumed"' in by_use
    assert "r-on" in selected and "r-on" not in by_use  # the chosen window is highlighted
    assert "current</span>" in recent
    assert '<span class="rt">+' in recent and '<span class="dim">→ ' in recent  # delta, then level


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
    assert thin[0].badge.startswith("partial, ") and thin[0].thin
    assert dash.history.rows[0].badge == ""  # sampled every two minutes: fully observed
    assert dash.chart.idle_minutes > 0 and dash.chart.gap_minutes > 0
    assert len(dash.chart.session_x) == 2
    html = render_app(dash)
    assert 'class="idle"' in html and 'fill="url(#gap)"' in html and 'class="sess"' in html
    assert 'class="r-thin"' in html


def test_a_server_side_correction_does_not_truncate_the_window(tmp_path) -> None:
    """The three consequences of the old drop-rule bug, at the level they bit."""
    e1 = T0 + SESSION_LENGTH_S
    rows = [(T0 + i * 60, 80 + i, e1) for i in range(0, 10)]
    rows += [(T0 + i * 60, 55 + i, e1) for i in range(10, 20)]  # corrected down 25 points
    windows = derive_sessions({"five_hour": rate_rows(rows)}, now=T0 + 3600)
    assert len(windows) == 1  # one window, not two
    assert windows[0].samples == 20  # counting every row, not truncated at the correction
    assert windows[0].started_at == T0 and windows[0].peak_pct == 89

    store = Store(tmp_path / "c.db")
    for ts, pct, ends in rows:
        store.record_quota(ts, [QuotaReading("five_hour", "5-hour", pct, iso(ends))])
    assert rebuild(store, now=T0 + 3600) == 1
    assert rebuild(store, now=T0 + 3600) == 1  # and the bad row cannot come back
    stored = store.sessions()
    assert len(stored) == 1 and stored[0]["samples"] == 20
    store.close()


# -- the incremental rebuild must agree with the full one ------------------------


def _generated_series() -> tuple[list[tuple[int, float, int]], list[tuple[int, float, str]], int]:
    """Three windows with a gap, an idle span, and a weekly reset inside window two."""
    step = 120
    e1 = T0 + SESSION_LENGTH_S
    e2 = e1 + 30 * 60 + SESSION_LENGTH_S  # half an hour idle, then a new window
    e3 = e2 + SESSION_LENGTH_S
    rate: list[tuple[int, float, int]] = []
    for i, ts in enumerate(range(T0, e1, step)):
        if e1 - 40 * 60 < ts < e1 - 20 * 60:
            continue  # a collection gap inside the first window
        rate.append((ts, min(90.0, i * 0.6), e1))
    for i, ts in enumerate(range(e2 - SESSION_LENGTH_S, e2, step)):
        rate.append((ts, min(70.0, i * 0.5), e2))
    now = e2 + 2 * 3600
    for i, ts in enumerate(range(e2, now + 1, step)):
        rate.append((ts, min(40.0, i * 0.4), e3))

    weekly: list[tuple[int, float, str]] = []
    for i, ts in enumerate(range(T0, now + 1, step)):
        if ts < e2 - 3600:
            weekly.append((ts, min(99.0, 40 + i * 0.05), "wk-1"))
        else:
            weekly.append((ts, min(30.0, i * 0.02), "wk-2"))  # the weekly limit resets
    return rate, weekly, now


def test_incremental_rebuild_matches_a_full_rebuild_row_for_row(tmp_path) -> None:
    rate, weekly, now = _generated_series()
    readings: dict[int, list[QuotaReading]] = {}
    for ts, pct, ends in rate:
        readings.setdefault(ts, []).append(QuotaReading("five_hour", "5-hour", pct, iso(ends)))
    for ts, pct, reset in weekly:
        readings.setdefault(ts, []).append(QuotaReading("seven_day", "7-day", pct, reset))

    incremental = Store(tmp_path / "inc.db")
    full = Store(tmp_path / "full.db")
    for ts in sorted(readings):
        incremental.record_quota(ts, readings[ts])
        full.record_quota(ts, readings[ts])
        rebuild_recent(incremental, now)  # what the poller does, every sample
    rebuild(full, now)  # what startup does, once

    got = incremental.sessions(limit=100)
    want = full.sessions(limit=100)
    assert len(want) == 3  # the series really does contain three windows
    assert [w["samples"] for w in want] == [w["samples"] for w in got]
    assert got == want  # every column of every row
    assert sum(w["is_current"] for w in got) == 1
    incremental.close()
    full.close()


def _history(store: Store, windows: int, now: int) -> None:
    """``windows`` back-to-back session windows at a two-minute cadence."""
    for w in range(windows):
        ends = now - (windows - 1 - w) * SESSION_LENGTH_S
        for i, ts in enumerate(range(ends - SESSION_LENGTH_S, ends, 120)):
            store.record_quota(ts, [QuotaReading("five_hour", "5-hour", i * 0.5, iso(ends))])


def _reads_for_one_incremental_pass(store: Store, now: int) -> int:
    reads: list[int] = []
    original = store.quota_series

    def counting(since_ts: int, **kwargs: object):
        rows = original(since_ts, **kwargs)
        reads.append(len(rows))
        return rows

    store.quota_series = counting  # type: ignore[method-assign]
    rebuild_recent(store, now)
    store.quota_series = original  # type: ignore[method-assign]
    return sum(reads)


def test_incremental_rebuild_cost_does_not_grow_with_history(tmp_path) -> None:
    """The point of the exercise: the poll stops getting slower every day."""
    now = T0 + 20 * SESSION_LENGTH_S
    small, large = Store(tmp_path / "small.db"), Store(tmp_path / "large.db")
    _history(small, 3, now)
    _history(large, 12, now)
    rebuild(small, now)
    rebuild(large, now)
    assert large.counts()["quota"] > 3 * small.counts()["quota"]

    small_reads = _reads_for_one_incremental_pass(small, now)
    large_reads = _reads_for_one_incremental_pass(large, now)
    assert small_reads == large_reads  # a two-window tail either way
    assert large_reads < large.counts()["quota"] / 3
    small.close()
    large.close()


# -- the inference is checked, not assumed ---------------------------------------


def test_the_watchdog_fires_when_a_window_is_extended_in_place() -> None:
    """A forward move under five hours with no drop: the fixed-window model is wrong."""
    base = T0 + SESSION_LENGTH_S
    detail = reset_model_violation(iso(base), 40.0, iso(base + 2 * 3600 + 300), 41.0)
    assert detail is not None
    assert "moved forward 2h 05m" in detail and "40% then 41%" in detail
    assert "may be wrong" in detail


def test_the_watchdog_stays_quiet_for_everything_consistent() -> None:
    base = T0 + SESSION_LENGTH_S
    # a genuine new window: five hours or more later, and the percentage drops
    assert reset_model_violation(iso(base), 90.0, iso(base + SESSION_LENGTH_S), 2.0) is None
    assert reset_model_violation(iso(base), 90.0, iso(base + 6 * 3600), 2.0) is None
    # sub-second jitter on the same window
    assert reset_model_violation(iso(base), 40.0, iso(base + 1), 41.0) is None
    # a forward move under five hours, but the percentage dropped: odd, not falsifying
    assert reset_model_violation(iso(base), 90.0, iso(base + 3600), 5.0) is None
    # backwards, unparsable or missing
    assert reset_model_violation(iso(base), 40.0, iso(base - 3600), 41.0) is None
    assert reset_model_violation(None, 40.0, iso(base), 41.0) is None
    assert reset_model_violation(iso(base), None, iso(base + 3600), 41.0) is None
    assert reset_model_violation("nonsense", 40.0, iso(base + 3600), 41.0) is None


# -- forgetting a window another collector wrote ---------------------------------


def _two_collectors(store, now: int) -> tuple[int, int]:
    """One real window, plus a second collector's short one interleaved second by second.

    This is the shape the shipped bug produced: a scratch instance pointed at the
    real database, writing its own five_hour expiry into the same minutes.
    """
    real_end = now + 3600
    for i in range(60):
        ts = now - (60 - i) * 60
        store.record_quota(
            ts,
            [
                QuotaReading("five_hour", "5-hour", 10 + i * 0.5, iso(real_end)),
                QuotaReading("seven_day", "7-day", 40.0, "wk"),
            ],
        )
    other_end = now + 1800  # a different expiry: a different window
    for i in range(4):
        store.record_quota(
            now - 300 + i * 30 + 2,  # +2s: between the real samples, not on top of them
            [QuotaReading("five_hour", "5-hour", 90.0 + i, iso(other_end))],
        )
    return real_end, other_end


def test_window_sample_ts_selects_by_expiry_not_by_time_range(settings, store) -> None:
    now = T0
    real_end, other_end = _two_collectors(store, now)
    from quotalens.sessions import window_sample_ts

    stray = window_sample_ts(store, other_end)
    real = window_sample_ts(store, real_end)

    assert len(stray) == 4
    assert len(real) == 60
    assert not set(stray) & set(real)
    # The stray samples sit inside the real window's span, so a range would take both.
    assert min(real) < min(stray) < max(stray) < max(real)


def test_forget_removes_one_window_and_leaves_the_interleaved_real_rows(settings, store) -> None:
    now = T0
    real_end, other_end = _two_collectors(store, now)
    from quotalens.sessions import window_sample_ts

    assert rebuild(store, now) == 2  # the real window and the stray one

    stray = window_sample_ts(store, other_end)
    result = store.forget_samples(stray, dry_run=True)
    assert result.samples == 0  # record_quota writes no raw sample rows
    assert result.quota_rows == 4
    assert store.forget_samples(stray).quota_rows == 4

    assert rebuild(store, now) == 1
    remaining = store.sessions(limit=10, order="recent")
    assert [int(w["started_at"]) for w in remaining] == [real_end - SESSION_LENGTH_S]
    assert int(remaining[0]["samples"]) == 60  # every real row survived


def test_forget_of_nothing_is_not_an_error(settings, store) -> None:
    result = store.forget_samples([])
    assert result.total == 0
