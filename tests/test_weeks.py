"""The Weeks ledger: one recorded event per weekly reset, with the cost as it stood.

Modelled on the boost tests. The hard part is the same shape: spotting the reset is
easy; the discipline is refusing to pin a week's cost on a reading the parser had to
recover, and being idempotent so a repeat start writes nothing.

The three-reset backfill against the real database is proved in the session summary,
not here — a suite must not depend on the machine's own history. These tests build the
windows by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from quotalens.budget import WindowCost, week_key
from quotalens.parse import QuotaReading
from quotalens.runway import SESSION_LENGTH_S
from quotalens.sessions import Delta, SessionWindow
from quotalens.store import QuotaRow
from quotalens.weeks import (
    WEEK_RESET_KIND,
    WeekReset,
    backfill,
    detect_reset,
    detect_resets,
    ledger,
    record_live,
    reset_slip_s,
    week_reset,
)

# A Monday, so the anchor arithmetic is legible. 2026-01-05 is a Monday.
MON_A = int(datetime(2026, 1, 5, 1, 0, 0, tzinfo=UTC).timestamp())  # opens week A
MON_B = MON_A + 7 * 86400  # opens week B, closes week A
MON_C = MON_B + 7 * 86400


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def row(window: str, pct: float, reset: str, ts: int) -> QuotaRow:
    return QuotaRow(ts, window, window, pct, reset)


def reading(window: str, pct: float, reset: str) -> QuotaReading:
    return QuotaReading(window, window, pct, reset)


# -- detection --------------------------------------------------------------------


def test_a_weekly_reset_is_detected() -> None:
    before = row("seven_day", 99.0, iso(MON_B), MON_B - 60)
    after = reading("seven_day", 0.0, iso(MON_C))
    transition = detect_reset(before, after)

    assert transition is not None
    assert transition.window == "seven_day"
    assert transition.closed_at == MON_B - 60
    assert (transition.closed_pct, transition.opened_pct) == (99.0, 0.0)
    assert transition.reset_at == iso(MON_B) and transition.next_reset_at == iso(MON_C)


def test_the_session_window_is_not_a_weekly_reset() -> None:
    """five_hour resets are the hero's business, not the ledger's."""
    before = row("five_hour", 90.0, iso(MON_B), MON_B - 60)
    after = reading("five_hour", 0.0, iso(MON_B + 5 * 3600))
    assert detect_reset(before, after) is None


def test_the_jitter_is_not_a_reset() -> None:
    """resets_at moved by under a minute: the server recomputing, not a new week."""
    before = row("seven_day", 40.0, iso(MON_B), MON_B - 60)
    after = reading("seven_day", 40.5, iso(MON_B + 0.8))
    assert detect_reset(before, after) is None


@pytest.mark.parametrize("undated", ["previous", "current"])
def test_an_undated_block_is_not_a_reset(undated: str) -> None:
    before = row("seven_day", 99.0, None if undated == "previous" else iso(MON_B), MON_B - 60)
    after = reading("seven_day", 0.0, None if undated == "current" else iso(MON_C))
    assert detect_reset(before, after) is None


def test_an_unverified_reading_never_becomes_a_reset() -> None:
    before = row("seven_day", 99.0, iso(MON_B), MON_B - 60)
    after = reading("seven_day", 0.0, iso(MON_C))
    assert detect_reset(before, after, trusted=False) is None


def test_detect_resets_reports_every_weekly_window() -> None:
    previous = [
        row("seven_day", 99.0, iso(MON_B), MON_B - 60),
        row("limit:fable", 82.0, iso(MON_B), MON_B - 60),
        row("five_hour", 40.0, iso(MON_B - 3600), MON_B - 60),
    ]
    readings = [
        reading("seven_day", 0.0, iso(MON_C)),
        reading("limit:fable", 1.0, iso(MON_C)),
        reading("five_hour", 41.0, iso(MON_B - 3600)),  # unchanged: not a reset
    ]
    found = detect_resets(previous, readings)
    assert sorted(t.window for t in found) == ["limit:fable", "seven_day"]


# -- reset slip -------------------------------------------------------------------


def test_reset_slip_is_the_deviation_from_seven_days() -> None:
    a = "2026-09-07T00:59:59.000000+00:00"
    b = "2026-09-14T00:59:59.830000+00:00"  # 7 days and 0.83s later
    assert reset_slip_s(a, b) == pytest.approx(0.83)


def test_a_changed_schedule_shows_up_in_the_slip() -> None:
    a = "2026-09-07T01:00:00+00:00"
    b = "2026-09-14T03:00:00+00:00"  # two hours late
    assert reset_slip_s(a, b) == pytest.approx(7200.0)


# -- the cost of the closed week, as it stood -------------------------------------


def cost(per_full: float) -> WindowCost:
    return WindowCost(MON_A, 100.0, per_full, per_full / 100.0)


def test_week_reset_carries_the_weeks_median_spread_and_both_reset_figures() -> None:
    transition = detect_reset(
        row("seven_day", 100.0, iso(MON_B), MON_B - 60), reading("seven_day", 0.0, iso(MON_C))
    )
    assert transition is not None
    week = [cost(x) for x in (8.0, 10.0, 12.0, 14.0, 20.0)]  # median 12, spread either side
    all_history = [cost(x) for x in (10.0, 10.0, 10.0, 10.0, 10.0)]  # all-history median 10
    reset = week_reset(transition, week, all_history)

    assert reset.usable_windows == 5
    assert reset.cost_per_full == pytest.approx(12.0)
    assert reset.cost_low < reset.cost_per_full < reset.cost_high
    # at last week's rate: 100 / the closed week's median; at reset: 100 / all-history median
    assert reset.full_windows_at_last_weeks_rate == pytest.approx(round(100.0 / 12.0, 2))
    assert reset.full_windows_at_reset == pytest.approx(10.0)


def test_below_the_floor_the_cost_is_unknown_not_zero() -> None:
    transition = detect_reset(
        row("seven_day", 100.0, iso(MON_B), MON_B - 60), reading("seven_day", 0.0, iso(MON_C))
    )
    assert transition is not None
    reset = week_reset(transition, [cost(10.0) for _ in range(4)])  # under MIN_COMPARE_WINDOWS
    assert reset.usable_windows == 4
    assert reset.cost_per_full is None and reset.full_windows_at_last_weeks_rate is None
    assert reset.full_windows_at_reset is None  # no all-history costs passed either


# -- JSON round-trip --------------------------------------------------------------


def test_the_json_detail_round_trips() -> None:
    reset = WeekReset(
        window="seven_day",
        label="Weekly — all models",
        closed_at=MON_B - 60,
        closed_pct=100.0,
        opened_pct=0.0,
        reset_at=iso(MON_B),
        next_reset_at=iso(MON_C),
        reset_slip=0.83,
        cost_per_full=11.76,
        cost_low=6.0,
        cost_high=19.05,
        usable_windows=41,
        full_windows_at_reset=8.42,
        full_windows_at_last_weeks_rate=11.1,
    )
    assert WeekReset.from_detail(reset.detail()) == reset


def test_a_detail_that_is_not_ours_is_ignored() -> None:
    assert WeekReset.from_detail("not json") is None
    assert WeekReset.from_detail('{"kind": "something else"}') is None


def test_an_older_event_maps_full_windows_left_to_last_weeks_rate() -> None:
    """A pre-26 event stored only full_windows_left; it becomes the last-week's-rate figure."""
    old = (
        '{"window":"seven_day","label":"Weekly","closed_at":' + str(MON_B - 60) + ','
        '"closed_pct":100.0,"opened_pct":0.0,"reset_at":"","next_reset_at":"",'
        '"reset_slip_s":0.5,"cost_per_full":9.0,"cost_low":8.0,"cost_high":11.0,'
        '"usable_windows":13,"full_windows_left":11.1}'
    )
    reset = WeekReset.from_detail(old)
    assert reset is not None
    assert reset.full_windows_at_last_weeks_rate == pytest.approx(11.1)
    assert reset.full_windows_at_reset is None


# -- backfill and the ledger ------------------------------------------------------


def _seed_reset(store, window: str, closed_pct: float) -> None:
    """One weekly window with a reset between week A and week B, plus week-A cost."""
    # Readings for the weekly window: several inside week A on reset R_B, then the
    # opening reading of week B on reset R_C. The change in resets_at is the reset.
    for i in range(5):
        store.record_quota(
            MON_B - (5 - i) * 60, [QuotaReading(window, window, 90.0 + i, iso(MON_B))]
        )
    store.record_quota(MON_B + 60, [QuotaReading(window, window, 0.0, iso(MON_C))])


def _week_a_windows(key: str) -> list[SessionWindow]:
    """Five complete session windows inside week A, each costing 10 points/full."""
    windows = []
    for i in range(5):
        started = MON_A + 3600 + i * (SESSION_LENGTH_S + 600)
        windows.append(
            SessionWindow(
                started_at=started,
                ends_at=started + SESSION_LENGTH_S,
                is_current=False,
                peak_pct=100.0,
                final_pct=100.0,
                samples=300,
                first_ts=started,
                last_ts=started + SESSION_LENGTH_S,
                deltas={key: Delta(10.0, 20.0, False)},
                covered_s=SESSION_LENGTH_S,
            )
        )
    return windows


def test_backfill_records_one_event_per_window_and_is_idempotent(settings, store) -> None:
    _seed_reset(store, "seven_day", 100.0)
    store.replace_sessions(_week_a_windows("seven_day"))
    now = MON_C + 86400

    first = backfill(store, now)
    assert [r.window for r in first] == ["seven_day"]
    assert first[0].closed_at == MON_B - 60
    assert first[0].cost_per_full == pytest.approx(10.0)  # week A cost, computed as it stood
    # both reset figures: at last week's rate (100/10) and at reset (100/all-history median 10)
    assert first[0].full_windows_at_last_weeks_rate == pytest.approx(10.0)
    assert first[0].full_windows_at_reset == pytest.approx(10.0)

    assert backfill(store, now) == [], "a second run must not write a duplicate"
    assert len(store.recent_events(limit=50, kind=WEEK_RESET_KIND)) == 1


def test_backfill_upgrades_a_pre26_event_to_carry_both_reset_figures(settings, store) -> None:
    """An old event with only full_windows_left is deleted and rewritten with the new schema."""
    import json

    _seed_reset(store, "seven_day", 100.0)
    store.replace_sessions(_week_a_windows("seven_day"))
    old = json.dumps(
        {
            "window": "seven_day",
            "label": "Weekly",
            "closed_at": MON_B - 60,
            "closed_pct": 100.0,
            "opened_pct": 0.0,
            "reset_at": iso(MON_B),
            "next_reset_at": iso(MON_C),
            "reset_slip_s": 0.5,
            "cost_per_full": 10.0,
            "cost_low": 8.0,
            "cost_high": 12.0,
            "usable_windows": 5,
            "full_windows_left": 10.0,  # pre-26 schema: no full_windows_at_reset
        }
    )
    store.record_event(WEEK_RESET_KIND, old, ts=MON_B - 60)

    backfill(store, MON_C + 86400)
    events = store.recent_events(limit=50, kind=WEEK_RESET_KIND)
    assert len(events) == 1, "the stale event is replaced, not duplicated"
    assert "full_windows_at_reset" in events[0].detail
    reset = WeekReset.from_detail(events[0].detail)
    assert reset.full_windows_at_reset == pytest.approx(10.0)


def test_backfill_skips_a_reset_the_parser_had_to_recover(settings, store) -> None:
    from quotalens.boost import SHAPE_DRIFT_KIND

    _seed_reset(store, "seven_day", 100.0)
    store.record_event(SHAPE_DRIFT_KIND, "parsed via generic fallback", ts=MON_B + 60)
    assert backfill(store, MON_C + 86400) == []


def test_backfill_on_a_database_with_no_resets_writes_nothing(settings, store) -> None:
    for i in range(10):
        store.record_quota(
            MON_A + i * 60, [QuotaReading("seven_day", "Weekly", 10.0 + i, iso(MON_B))]
        )
    assert backfill(store, MON_B) == []
    assert store.recent_events(limit=10, kind=WEEK_RESET_KIND) == []


def test_the_ledger_reads_recorded_events_newest_first(settings, store) -> None:
    _seed_reset(store, "seven_day", 100.0)
    _seed_reset(store, "limit:fable", 82.0)
    store.replace_sessions(_week_a_windows("seven_day") + _week_a_windows("limit:fable"))
    backfill(store, MON_C + 86400)

    rows = ledger(store)
    assert {r.window for r in rows} == {"seven_day", "limit:fable"}
    assert all(isinstance(r, WeekReset) for r in rows)


def test_record_live_writes_the_reset_from_the_poll_path(settings, store) -> None:
    store.replace_sessions(_week_a_windows("seven_day"))
    previous = [row("seven_day", 100.0, iso(MON_B), MON_B - 60)]
    readings = [reading("seven_day", 0.0, iso(MON_C))]

    written = record_live(store, previous, readings, MON_C + 86400, trusted=True)
    assert [r.window for r in written] == ["seven_day"]
    assert len(store.recent_events(limit=10, kind=WEEK_RESET_KIND)) == 1

    # A fallback-recovered poll makes no claim, and the already-recorded one is not doubled.
    assert record_live(store, previous, readings, MON_C + 86400, trusted=False) == []
    assert record_live(store, previous, readings, MON_C + 86400, trusted=True) == []


# -- week_key ---------------------------------------------------------------------


def test_week_key_anchors_on_monday_01_utc() -> None:
    assert week_key(MON_A) == "2026-01-05"
    assert week_key(MON_A + 3600) == "2026-01-05"
    assert week_key(MON_A - 3600) == "2025-12-29", "just before Monday 01:00 is the prior week"
    assert week_key(MON_B - 1) == "2026-01-05", "the last second of the week is still the week"


# -- the ledger rows, the verdict, and the export ---------------------------------


def _reset(
    window: str, closed_at: int, closed_pct: float, cost, low, high, n, at_reset, at_rate
) -> WeekReset:
    return WeekReset(
        window=window,
        label=window,
        closed_at=closed_at,
        closed_pct=closed_pct,
        opened_pct=0.0,
        reset_at=iso(closed_at + 60),
        next_reset_at=iso(closed_at + 60 + 7 * 86400),
        reset_slip=0.5,
        cost_per_full=cost,
        cost_low=low,
        cost_high=high,
        usable_windows=n,
        full_windows_at_reset=at_reset,
        full_windows_at_last_weeks_rate=at_rate,
    )


def _record(store, reset: WeekReset) -> None:
    store.record_event(WEEK_RESET_KIND, reset.detail(), ts=reset.closed_at)


def test_the_reset_figure_lands_on_the_opened_week_not_the_closed_one(settings, store) -> None:
    from quotalens.weeks import week_rows

    close = MON_B - 60  # in week A; this reset closes week A and opens week B
    _record(store, _reset("seven_day", close, 100.0, 12.0, 11.0, 13.0, 11, 8.5, 8.3))
    _record(store, _reset("limit:fable", close, 82.0, 12.8, 11.0, 14.5, 8, None, 7.8))

    rows = {r["week"]: r for r in week_rows(store)}
    week_a, week_b = week_key(close), week_key(MON_B + 3600)
    # The cost and percent-used live on the week that CLOSED.
    assert rows[week_a]["weekly_all_cost"] == 12.0 and rows[week_a]["fable_cost"] == 12.8
    assert rows[week_a]["closed_pct"] == 100.0 and rows[week_a]["left_unused_pct"] == 0.0
    assert rows[week_a]["full_windows_at_reset"] is None  # nothing opened week A here
    # The reset figures live on the week that OPENED.
    assert rows[week_b]["full_windows_at_reset"] == 8.5
    assert rows[week_b]["full_windows_at_last_weeks_rate"] == 8.3
    assert rows[week_b]["weekly_all_cost"] is None  # week B has not closed yet


def test_verdict_reads_in_the_readers_units_when_ranges_are_disjoint() -> None:
    from quotalens.weeks import verdict

    rows = [
        {"weekly_all_cost": 9.0, "weekly_all_low": 7.1, "weekly_all_high": 11.0},
        {"weekly_all_cost": 12.0, "weekly_all_low": 11.5, "weekly_all_high": 13.0},
    ]
    assert verdict(rows) == (
        "A full session used 9% of the week, against 12% the week before. "
        "The usual ranges do not overlap: a session is cheaper this week."
    )


def test_verdict_says_no_change_when_ranges_overlap() -> None:
    from quotalens.weeks import verdict

    rows = [
        {"weekly_all_cost": 9.0, "weekly_all_low": 7.1, "weekly_all_high": 11.1},
        {"weekly_all_cost": 12.0, "weekly_all_low": 10.0, "weekly_all_high": 14.8},
    ]
    assert verdict(rows) == (
        "A full session used 9% of the week, against 12% the week before. "
        "The usual ranges overlap, so no change can be called."
    )


def test_verdict_needs_two_complete_weeks() -> None:
    from quotalens.weeks import verdict

    assert verdict([]) == "fewer than two complete weeks"
    one = [{"weekly_all_cost": 9.0, "weekly_all_low": 7.0, "weekly_all_high": 11.0}]
    assert verdict(one) == "fewer than two complete weeks"


def test_the_api_and_export_serve_the_same_rows(settings, store, secrets) -> None:
    import csv
    import io

    from fastapi.testclient import TestClient

    from quotalens.api import create_app

    _record(store, _reset("seven_day", MON_B - 60, 100.0, 12.0, 11.5, 13.0, 11, 8.2, 8.3))
    _record(store, _reset("seven_day", MON_C - 60, 99.0, 9.0, 7.1, 11.1, 13, 8.5, 11.1))
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"

    with TestClient(app) as tc:
        api = tc.get("/api/weeks").json()
        export = tc.get("/api/export.json?table=weeks").json()
        rows_csv = list(csv.DictReader(io.StringIO(tc.get("/api/export.csv?table=weeks").text)))

    weeks_api = api["weeks"]
    # A row per closed week plus the week the newest reset opened: three in all, newest first.
    assert len(weeks_api) == 3
    opened_by_newest = week_key(MON_C - 60 + 7 * 86400)
    top = next(r for r in weeks_api if r["week"] == opened_by_newest)
    assert top["full_windows_at_reset"] == 8.5 and top["full_windows_at_last_weeks_rate"] == 11.1
    assert top["weekly_all_cost"] is None  # opened but not yet closed
    assert "do not overlap" in api["verdict"] and "cheaper this week" in api["verdict"]
    # Same rows in the export, oldest-first, and the CSV carries the new columns.
    assert [r["week"] for r in export["rows"]] == [r["week"] for r in reversed(weeks_api)]
    assert len(rows_csv) == 3 and "full_windows_at_last_weeks_rate" in rows_csv[0]


# -- rendering the section --------------------------------------------------------


def _render(rows, verdict_text: str):
    from quotalens.dashboard import Dashboard, WeekRowView, WeeksView
    from quotalens.render import _weeks

    dash = object.__new__(Dashboard)
    dash.weeks = WeeksView([WeekRowView(*r) for r in rows], verdict_text, "note text")
    return _weeks(dash)


# WeekRowView fields: week_label, closed_text, used_primary, used_secondary, fable_primary,
# fable_secondary, used_text, left_text, reset_primary, reset_secondary, is_open
def test_the_section_renders_in_readable_units_with_a_fable_line() -> None:
    open_row = (
        "21 Sep – 28 Sep · this week", "collecting", "collecting", "", "Fable collecting", "",
        "—", "—", "8.5", "11.1 at last week's rate", True,
    )
    week_c = (
        "14 Sep – 21 Sep", "21 Sep 06:29", "9% of week", "usually 8–11% · 13 sessions",
        "Fable 9%", "usually 4–14% · 13 sessions", "100%", "0%", "7.8",
        "7.8 at last week's rate", False,
    )
    verdict_text = (
        "A full session used 9% of the week, against 12% the week before. "
        "The usual ranges overlap, so no change can be called."
    )
    html = _render([open_row, week_c], verdict_text)

    assert "One full session used" in html and "Full sessions at reset" in html
    assert "9% of week" in html and "usually 8–11% · 13 sessions" in html
    assert "Fable 9%" in html  # the Fable line in the same cell
    assert "8.5" in html and "11.1 at last week&#x27;s rate" in html
    assert "no change can be called" in html
    assert html.count("<tr class") == 2


def test_the_section_with_only_the_open_week_shows_collecting_and_says_insufficient() -> None:
    open_row = (
        "21 Sep – 28 Sep · this week", "collecting", "collecting", "", "", "",
        "—", "—", "—", "", True,
    )
    html = _render([open_row], "fewer than two complete weeks")
    assert "collecting" in html
    assert "fewer than two complete weeks" in html


def test_an_empty_ledger_renders_nothing() -> None:
    from quotalens.dashboard import Dashboard
    from quotalens.render import _weeks

    dash = object.__new__(Dashboard)
    dash.weeks = None
    assert _weeks(dash) == ""


def test_weeks_mostly_column(settings, store, secrets) -> None:
    from fastapi.testclient import TestClient

    from quotalens.api import create_app
    from quotalens.parse import SurfaceBreakdown, SurfaceShare

    _record(store, _reset("seven_day", MON_B - 60, 100.0, 12.0, 11.5, 13.0, 11, 8.2, 8.3))
    _record(store, _reset("seven_day", MON_C - 60, 99.0, 9.0, 7.1, 11.1, 13, 8.5, 11.1))
    started = datetime.fromtimestamp(MON_B, UTC).isoformat()  # the week MON_C closes
    early = [SurfaceShare("chat", "Chats", 60.0), SurfaceShare("cowork", "Cowork", 40.0)]
    final = [SurfaceShare("chat", "Chats", 24.0), SurfaceShare("cowork", "Cowork", 76.0)]
    store.record_breakdown(MON_B + 3600, SurfaceBreakdown(None, started, early))
    # the last split stored is the one at the close
    store.record_breakdown(MON_C - 120, SurfaceBreakdown(None, started, final))
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"

    with TestClient(app) as tc:
        rows = {r["week"]: r for r in tc.get("/api/weeks").json()["weeks"]}
        html = tc.get("/").text
    assert rows[week_key(MON_B)]["mostly"] == {"label": "Cowork", "percent": 76.0}
    assert rows[week_key(MON_A)]["mostly"] is None  # before the block was collected
    assert "<th" in html and ">Mostly</th>" in html
    assert '<td class="n">Cowork 76%</td>' in html
    assert '<td class="n">—</td></tr>' in html
