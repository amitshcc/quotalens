"""WP-31: the week view's extras -- last week's line under this week's.

The overlay is weekly-all only, drawn on the Monday-to-Monday axis of ``range=week``,
and toggled by a ``hide=vs-last-week`` key so the picker's own localStorage entry
persists it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from quotalens.api import create_app
from quotalens.dashboard import GHOST_KEY, _series_chips
from quotalens.parse import QuotaReading
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
