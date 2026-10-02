"""The series picker (§4) and the "this week" / "last week" ranges (§5).

The picker chips are real links onto the existing hide= URLs: a plain click isolates a
series, a shift-click toggles one, All clears every hide. The week ranges are not
durations but the current and previous weekly windows, resolved from the newest weekly
resets_at, and a weekly-only selection with an auto range resolves to the current week.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from quotalens.api import create_app
from quotalens.dashboard import SeriesChip, _series_chips
from quotalens.parse import QuotaReading
from quotalens.views import RANGE_KEYS, ViewOptions, resolve_range

NOW = 1_800_000_000
WEEK = 7 * 86400
THIS_WEEK = (NOW - WEEK, NOW)
LAST_WEEK = (NOW - 2 * WEEK, NOW - WEEK)


# -- §5 the week ranges -----------------------------------------------------------


def test_week_and_lastweek_are_selectable_range_keys() -> None:
    assert "week" in RANGE_KEYS and "lastweek" in RANGE_KEYS


def test_range_week_is_the_current_weekly_window() -> None:
    r = resolve_range(ViewOptions(range_key="week"), NOW - 30 * 86400, NOW, this_week=THIS_WEEK)
    assert (r.start, r.end, r.key) == (*THIS_WEEK, "week")
    assert not r.auto and r.label == "this week"


def test_range_lastweek_is_the_previous_weekly_window() -> None:
    r = resolve_range(ViewOptions(range_key="lastweek"), NOW - 30 * 86400, NOW, last_week=LAST_WEEK)
    assert (r.start, r.end, r.key) == (*LAST_WEEK, "lastweek")


def test_a_weekly_only_view_with_auto_resolves_to_this_week() -> None:
    """Session hidden, only weekly series shown: the Monday-to-Monday axis is what is wanted."""
    r = resolve_range(
        ViewOptions(range_key="auto"),
        NOW - 30 * 86400,
        NOW,
        session=(NOW - 3600, NOW + 3600),  # a session is running, but it is hidden
        this_week=THIS_WEEK,
        weekly_only=True,
    )
    assert r.key == "week" and r.auto and (r.start, r.end) == THIS_WEEK


def test_auto_still_prefers_the_session_when_it_is_not_a_weekly_only_view() -> None:
    r = resolve_range(
        ViewOptions(range_key="auto"),
        NOW - 30 * 86400,
        NOW,
        session=(NOW - 3600, NOW + 3600),
        this_week=THIS_WEEK,
        weekly_only=False,
    )
    assert r.key == "session"


def test_week_falls_back_when_no_weekly_reset_is_known() -> None:
    """No weekly data, no week window: it must not crash, just pick a sane default."""
    r = resolve_range(ViewOptions(range_key="week"), NOW - 30 * 86400, NOW, this_week=None)
    assert r.key in RANGE_KEYS or r.key in {"session", "custom"}


# -- §4 the series chips ----------------------------------------------------------

ORDER = ["five_hour", "seven_day", "limit:fable"]
LABELS = {"five_hour": "Session", "seven_day": "Weekly — all models", "limit:fable": "Fable"}


def test_chips_are_all_then_one_per_series_in_order() -> None:
    chips = _series_chips(ORDER, LABELS, ViewOptions())
    assert [c.key for c in chips] == ["", "five_hour", "seven_day", "limit:fable"]
    assert [c.label for c in chips] == ["All", "Session", "Weekly all", "Weekly Fable"]


def test_all_is_active_only_when_nothing_is_hidden() -> None:
    assert _series_chips(ORDER, LABELS, ViewOptions())[0].active
    assert not _series_chips(ORDER, LABELS, ViewOptions(hidden=frozenset({"five_hour"})))[0].active


def test_a_chips_plain_link_isolates_its_series() -> None:
    chips = {c.key: c for c in _series_chips(ORDER, LABELS, ViewOptions())}
    # Showing seven_day alone hides the other two.
    assert "hide=" in chips["seven_day"].href
    q = chips["seven_day"].href.split("hide=")[1]
    assert "five_hour" in q and "limit" in q and "seven_day" not in q.replace("%3A", ":")


def test_a_chips_toggle_link_flips_just_that_one() -> None:
    view = ViewOptions(hidden=frozenset({"five_hour"}))
    chips = {c.key: c for c in _series_chips(ORDER, LABELS, view)}
    # five_hour is hidden, so its toggle shows it again: no hide= for it.
    assert "five_hour" not in chips["five_hour"].toggle_href
    # seven_day is shown, so its toggle hides it in addition to five_hour.
    assert "seven_day" in chips["seven_day"].toggle_href


def test_active_reflects_what_is_visible() -> None:
    view = ViewOptions(hidden=frozenset({"five_hour"}))
    chips = {c.key: c for c in _series_chips(ORDER, LABELS, view)}
    assert not chips["five_hour"].active
    assert chips["seven_day"].active and chips["limit:fable"].active


# -- §4 rendering, and the raw key never leaking ----------------------------------


def _render_picker(chips: list[SeriesChip]) -> str:
    from quotalens.dashboard import Dashboard
    from quotalens.render import _series_picker

    dash = object.__new__(Dashboard)
    dash.series_chips = chips
    return _series_picker(dash)


def test_the_picker_renders_the_chips_and_never_a_raw_key() -> None:
    chips = _series_chips(ORDER, LABELS, ViewOptions(hidden=frozenset({"five_hour"})))
    html = _render_picker(chips)
    assert ">All</a>" in html and ">Weekly Fable</a>" in html
    assert 'aria-current="true"' in html  # the visible ones are marked active
    assert "data-toggle-href" in html
    assert "limit:fable" not in html, "the raw window key is never printed; hrefs encode it"


# -- §5 end to end through the dashboard ------------------------------------------


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _seed_weekly(store, now: int) -> None:
    reset = _iso(now + 2 * 86400)  # the weekly window resets two days out
    for i in range(30):
        ts = now - (29 - i) * 60
        store.record_quota(
            ts,
            [
                QuotaReading("five_hour", "5-hour", 20 + i, _iso(now + 3600), "normal", True),
                QuotaReading("seven_day", "7-day", 40, reset, "normal", False),
            ],
        )


def test_range_week_runs_monday_to_monday_through_the_dashboard(settings, store, secrets) -> None:
    now = int(time.time())
    _seed_weekly(store, now)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now

    with TestClient(app) as tc:
        body = tc.get("/api/dashboard?range=week").json()
    reset_ts = int(datetime.fromisoformat(_iso(now + 2 * 86400)).timestamp())
    assert body["range"]["key"] == "week"
    assert body["range"]["end"] == reset_ts
    assert body["range"]["start"] == reset_ts - WEEK


def test_hiding_the_session_resolves_auto_to_the_week(settings, store, secrets) -> None:
    now = int(time.time())
    _seed_weekly(store, now)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now

    with TestClient(app) as tc:
        body = tc.get("/api/dashboard?hide=five_hour").json()
    assert body["range"]["key"] == "week" and body["range"]["auto"] is True
