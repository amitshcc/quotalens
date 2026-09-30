"""WP-31: the week view's extras -- last week's line under this week's.

The overlay is weekly-all only, drawn on the Monday-to-Monday axis of ``range=week``,
and toggled by a ``hide=vs-last-week`` key so the picker's own localStorage entry
persists it.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from quotalens.api import create_app
from quotalens.dashboard import GHOST_KEY, _series_chips
from quotalens.heatmap import compute_heatmap
from quotalens.pace import HIDDEN_EARLY, HIDDEN_NO_HISTORY, HIDDEN_WITHHELD, compute_pace
from quotalens.parse import QuotaReading
from quotalens.render import _heatmap as render_heatmap
from quotalens.views import ViewOptions

WEEK = 7 * 86400


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _seed_two_weeks(store, now: int) -> int:
    """Last week's weekly-all climb, then this week's so far. Returns this week's reset."""
    reset = (now // 3600) * 3600 + 2 * 86400  # this weekly window resets two days out
    start = reset - WEEK
    for h in range(0, 168, 2):  # last week, every two hours, 0 -> 84%
        ts = start - WEEK + h * 3600
        store.record_quota(
            ts, [QuotaReading("seven_day", "7-day", h / 2, _iso(start), "normal", False)]
        )
    for i in range(30):  # this week, the last half hour
        ts = now - (29 - i) * 60
        store.record_quota(
            ts,
            [
                QuotaReading("five_hour", "5-hour", 20 + i, _iso(now + 3600), "normal", True),
                QuotaReading("seven_day", "7-day", 40, _iso(reset), "normal", False),
            ],
        )
    return reset


def _app(settings, store, secrets, now: int):
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    return app


def test_week_ghost_present(settings, store, secrets) -> None:
    now = int(time.time())
    _seed_two_weeks(store, now)
    with TestClient(_app(settings, store, secrets, now)) as tc:
        html = tc.get("/?range=week").text
        body = tc.get("/api/dashboard?range=week").json()
    assert body["ghost"] is True
    assert '<path d="M' in html and 'class="series-ghost"' in html
    assert ">vs last week</a>" in html  # the chip, in the picker row
    # Beneath the hatching and the boost marks: the ghost is the first thing drawn.
    trace = html.split('<g class="trace">', 1)[1]
    assert trace.startswith('<g aria-label="Weekly all, last week">')
    assert ">last week</text>" in trace  # labelled at its own end, never a swatch legend


def test_week_ghost_absent_outside_week_range(settings, store, secrets) -> None:
    now = int(time.time())
    _seed_two_weeks(store, now)
    with TestClient(_app(settings, store, secrets, now)) as tc:
        for q in ("range=24h", "range=7d", "range=lastweek", "range=all"):
            html = tc.get(f"/?{q}").text
            assert "series-ghost" not in html, q
            assert "vs last week" not in html, q
        # On the week range, the chip turns it off; the chip stays to turn it back on.
        off = tc.get(f"/?range=week&hide={GHOST_KEY}").text
        assert "series-ghost" not in off and ">vs last week</a>" in off
        # Hiding weekly-all hides its ghost with it.
        assert "series-ghost" not in tc.get("/?range=week&hide=seven_day").text


def test_picking_a_series_keeps_the_ghost_toggle() -> None:
    order = ["five_hour", "seven_day"]
    labels = {"five_hour": "Session", "seven_day": "Weekly — all models"}
    view = ViewOptions(range_key="week", hidden=frozenset({GHOST_KEY}))
    chips = {c.key: c for c in _series_chips(order, labels, view)}
    assert chips[""].active, "All is about series; the overlay being off does not unset it"
    assert GHOST_KEY in chips[""].href
    assert GHOST_KEY in chips["seven_day"].href


# -- task 2: the pace projection -------------------------------------------------

HOUR = 3600


def _seed_history(store, reset: int, prior_close: float, now_pct: float, elapsed_h: int) -> int:
    """Three complete prior weeks climbing evenly to ``prior_close``, then this week so far.

    Returns ``now``. Readings every hour, with this week's reset on every current row.
    """
    start = reset - WEEK
    for k in (3, 2, 1):
        w_start = start - k * WEEK
        for h in range(0, 168):
            pct = round(prior_close * h / 167)
            store.record_quota(
                w_start + h * HOUR,
                [QuotaReading("seven_day", "7-day", pct, _iso(w_start + WEEK), "normal", False)],
            )
    for h in range(0, elapsed_h + 1):
        pct = round(now_pct * h / elapsed_h) if elapsed_h else now_pct
        store.record_quota(
            start + h * HOUR,
            [QuotaReading("seven_day", "7-day", pct, _iso(reset), "normal", False)],
        )
    return start + elapsed_h * HOUR


RESET = 1_790_557_200 + WEEK  # a Monday 01:00Z


def test_pace_hidden_first_24h(store) -> None:
    now = _seed_history(store, RESET, prior_close=90, now_pct=15, elapsed_h=23)
    p = compute_pace(store, now)
    assert not p.shown and p.reason == HIDDEN_EARLY and p.sentence == ""
    # One hour later it shows, and says it is an estimate.
    store.record_quota(
        now + HOUR, [QuotaReading("seven_day", "7-day", 16, _iso(RESET), "normal", False)]
    )
    later = compute_pace(store, now + HOUR)
    assert later.shown and later.basis.startswith("An estimate from how the last 3 complete")


def test_pace_runs_out(store) -> None:
    # 50% two days in, and every prior week used another ~70 points from here on.
    now = _seed_history(store, RESET, prior_close=100, now_pct=50, elapsed_h=48)
    p = compute_pace(store, now)
    assert p.shown and p.runs_out and p.end_pct == 100
    assert p.runs_out_low_ts <= p.runs_out_ts <= RESET
    assert re.fullmatch(
        r"At this pace the week runs out around \w{3} \d\d:\d\d "
        r"\(\w{3} \d\d:\d\d – (\w{3} \d\d:\d\d|not before the reset)\)\.",
        p.sentence,
    ), p.sentence
    # Prior weeks went on to use 100 - 29 = 71 points from hour 48: 50 + 71 crosses 100
    # at hour 48 + 50/(100/167) ~= 131.5, so hour 132 of the prior climb.
    assert p.runs_out_ts == RESET - WEEK + 132 * HOUR
    body = p.as_dict()
    assert body["estimate"] is True and body["weeks_used"] == 3


def test_pace_finishes_under_100(store) -> None:
    # 20% three days in; prior weeks closed at 60 and used 60 - 26 = 34 from hour 72.
    now = _seed_history(store, RESET, prior_close=60, now_pct=20, elapsed_h=72)
    p = compute_pace(store, now)
    assert p.shown and not p.runs_out and p.runs_out_ts is None
    assert p.end_pct == pytest.approx(20 + 60 - round(60 * 72 / 167))
    assert p.end_low <= p.end_pct <= p.end_high < 100
    assert p.sentence == (
        f"At this pace the week ends near {p.end_pct:.0f}% ({p.end_low:.0f}–{p.end_high:.0f}%)."
    )


def test_pace_ignores_incomplete_and_boosted_weeks(store) -> None:
    start = RESET - WEEK
    # Last week: the collector joined 60 hours in -- not a complete week.
    for h in range(60, 168):
        store.record_quota(
            start - WEEK + h * HOUR,
            [QuotaReading("seven_day", "7-day", h / 2, _iso(start), "normal", False)],
        )
    # The week before: complete, but boosted -- 90 -> 1 on day four, reset unchanged.
    for h in range(0, 168):
        store.record_quota(
            start - 2 * WEEK + h * HOUR,
            [
                QuotaReading(
                    "seven_day",
                    "7-day",
                    h / 2 if h < 96 else 1,
                    _iso(start - WEEK),
                    "normal",
                    False,
                )
            ],
        )
    for h in range(0, 49):
        store.record_quota(
            start + h * HOUR,
            [QuotaReading("seven_day", "7-day", h / 2, _iso(RESET), "normal", False)],
        )
    p = compute_pace(store, start + 48 * HOUR)
    assert not p.shown and p.reason == HIDDEN_NO_HISTORY


def test_pace_sits_under_the_budget_table(settings, store, secrets) -> None:
    now = int(time.time())
    reset = (now // HOUR) * HOUR + 2 * 86400
    _seed_history(store, reset, prior_close=100, now_pct=50, elapsed_h=5 * 24 - 2)
    with TestClient(_app(settings, store, secrets, now)) as tc:
        html = tc.get("/").text
    budget = html.split('<section class="screen budget">', 1)[1].split("</section>", 1)[0]
    assert '<p class="pace">At this pace the week ' in budget
    assert "An estimate from how the last 3 complete weeks" in budget


# -- task 3: the heatmap ---------------------------------------------------------


def _seed_weeks_with_a_burst(store, n_weeks: int, burst_offset_s: int) -> list[int]:
    """``n_weeks`` complete weeks read every 10 minutes: flat, except +2 points at one
    moment ``burst_offset_s`` into each week, and +1 more twenty minutes later.
    Returns the burst timestamps."""
    start = RESET - WEEK
    bursts = []
    for k in range(n_weeks, 0, -1):
        w_start = start - k * WEEK
        burst = w_start + burst_offset_s
        bursts.append(burst)
        for t in range(w_start, w_start + WEEK, 600):
            pct = 10 + (2 if t >= burst else 0) + (1 if t >= burst + 1200 else 0)
            store.record_quota(
                t, [QuotaReading("seven_day", "7-day", pct, _iso(w_start + WEEK), "normal", False)]
            )
    # This week has begun, so the ones above are behind it.
    store.record_quota(
        start + HOUR, [QuotaReading("seven_day", "7-day", 1, _iso(RESET), "normal", False)]
    )
    return bursts


def test_heatmap_shape(store) -> None:
    # 50 h 5 min into the week: the burst and the +1 after it land in one local hour.
    bursts = _seed_weeks_with_a_burst(store, 2, 50 * HOUR + 300)
    heat = compute_heatmap(store, RESET - WEEK + 2 * HOUR)
    assert not heat.collecting and heat.weeks_used == 2
    assert len(heat.cells) == 7 and all(len(row) == 24 for row in heat.cells)
    local = datetime.fromtimestamp(bursts[0]).astimezone()
    day, hour = local.weekday(), local.hour
    assert heat.cells[day][hour] == pytest.approx(3.0)  # 3 points each week, averaged
    others = [
        v for d, row in enumerate(heat.cells) for h, v in enumerate(row) if (d, h) != (day, hour)
    ]
    assert all(v == 0 for v in others), "nothing else gained, and every hour was collected"
    body = heat.as_dict()
    assert [r["day"] for r in body["rows"]] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert body["rows"][day]["hours"][hour] == 3.0 and body["unit"] == "points per hour"
    # Rendered: 168 cells, a legend in points per hour, and no amber.
    html = render_heatmap(heat)
    assert html.count("<rect x=") == 168 + 1 + 4
    assert ">points/hour</text>" in html and "--s1" not in html and "--lit" not in html


def test_heatmap_collecting(store) -> None:
    _seed_weeks_with_a_burst(store, 1, 50 * HOUR)
    heat = compute_heatmap(store, RESET - WEEK + 2 * HOUR)
    assert heat.collecting and heat.weeks_used == 1
    html = render_heatmap(heat)
    assert "When you use it" in html and "Collecting: 1 of 2 complete weeks" in html
    assert "<svg" not in html


# -- task 4: the API ---------------------------------------------------------------


def test_api_pace_and_heatmap(settings, store, secrets) -> None:
    now = int(time.time())
    reset = (now // HOUR) * HOUR + 2 * 86400
    _seed_history(store, reset, prior_close=100, now_pct=50, elapsed_h=5 * 24 - 2)
    with TestClient(_app(settings, store, secrets, now)) as tc:
        pace = tc.get("/api/pace").json()
        heat = tc.get("/api/heatmap").json()
    assert pace["shown"] and pace["estimate"] is True and pace["window"] == "seven_day"
    assert pace["end_low"] <= pace["end_pct"] <= pace["end_high"]
    assert pace["sentence"].startswith("At this pace the week ")
    assert set(pace) >= {"runs_out", "runs_out_ts", "runs_out_low_ts", "runs_out_high_ts"}
    assert not heat["collecting"] and heat["weeks_used"] == 3
    assert len(heat["rows"]) == 7 and all(len(r["hours"]) == 24 for r in heat["rows"])
    assert heat["unit"] == "points per hour"


def test_api_pace_is_hidden_while_the_collector_is_not_ok(settings, store, secrets) -> None:
    now = int(time.time())
    reset = (now // HOUR) * HOUR + 2 * 86400
    _seed_history(store, reset, prior_close=100, now_pct=50, elapsed_h=5 * 24 - 2)
    app = create_app(settings, store, secrets)  # never polled: readings are not trusted
    with TestClient(app) as tc:
        pace = tc.get("/api/pace").json()
        assert tc.get("/api/heatmap").json()["collecting"] is False
    assert pace["shown"] is False and pace["reason"] == HIDDEN_WITHHELD
