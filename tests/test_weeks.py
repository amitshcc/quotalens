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


def test_week_reset_carries_the_weeks_median_spread_and_full_sessions() -> None:
    transition = detect_reset(
        row("seven_day", 100.0, iso(MON_B), MON_B - 60), reading("seven_day", 0.0, iso(MON_C))
    )
    assert transition is not None
    costs = [cost(x) for x in (8.0, 10.0, 12.0, 14.0, 20.0)]  # median 12, spread either side
    reset = week_reset(transition, costs)

    assert reset.usable_windows == 5
    assert reset.cost_per_full == pytest.approx(12.0)
    assert reset.cost_low is not None and reset.cost_high is not None
    assert reset.cost_low < reset.cost_per_full < reset.cost_high
    assert reset.full_windows_left == pytest.approx(round(100.0 / 12.0, 2))


def test_below_the_floor_the_cost_is_unknown_not_zero() -> None:
    transition = detect_reset(
        row("seven_day", 100.0, iso(MON_B), MON_B - 60), reading("seven_day", 0.0, iso(MON_C))
    )
    assert transition is not None
    reset = week_reset(transition, [cost(10.0) for _ in range(4)])  # under MIN_COMPARE_WINDOWS
    assert reset.usable_windows == 4
    assert reset.cost_per_full is None and reset.full_windows_left is None


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
        full_windows_left=8.42,
    )
    assert WeekReset.from_detail(reset.detail()) == reset


def test_a_detail_that_is_not_ours_is_ignored() -> None:
    assert WeekReset.from_detail("not json") is None
    assert WeekReset.from_detail('{"kind": "something else"}') is None


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
    assert first[0].full_windows_left == pytest.approx(10.0)  # a fresh week buys 100/10

    assert backfill(store, now) == [], "a second run must not write a duplicate"
    assert len(store.recent_events(limit=50, kind=WEEK_RESET_KIND)) == 1


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


def _reset(window: str, closed_at: int, closed_pct: float, cost, low, high, n, full) -> WeekReset:
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
        full_windows_left=full,
    )


def _record(store, reset: WeekReset) -> None:
    store.record_event(WEEK_RESET_KIND, reset.detail(), ts=reset.closed_at)


def test_week_rows_join_the_two_windows_of_one_reset(settings, store) -> None:
    from quotalens.weeks import week_rows

    close = MON_B - 60
    _record(store, _reset("seven_day", close, 100.0, 12.0, 11.0, 13.0, 11, 8.3))
    _record(store, _reset("limit:fable", close, 82.0, 12.8, 11.0, 14.5, 8, 7.8))

    rows = week_rows(store)
    assert len(rows) == 1
    r = rows[0]
    assert r["weekly_all_cost"] == 12.0 and r["weekly_all_n"] == 11
    assert r["fable_cost"] == 12.8 and r["fable_n"] == 8
    assert r["closed_pct"] == 100.0 and r["left_unused_pct"] == 0.0
    assert r["full_windows_left"] == 8.3  # the weekly-all figure, not Fable's


def test_verdict_says_do_not_overlap_when_the_iqrs_are_disjoint() -> None:
    from quotalens.weeks import verdict

    rows = [
        {"weekly_all_cost": 9.0, "weekly_all_low": 7.1, "weekly_all_high": 11.0},
        {"weekly_all_cost": 12.0, "weekly_all_low": 11.5, "weekly_all_high": 13.0},
    ]
    assert verdict(rows) == "Weekly cost per session: 9 vs 12 last week; ranges do not overlap."


def test_verdict_says_overlap_when_the_iqrs_share_a_value() -> None:
    from quotalens.weeks import verdict

    rows = [
        {"weekly_all_cost": 9.0, "weekly_all_low": 7.1, "weekly_all_high": 11.1},
        {"weekly_all_cost": 12.0, "weekly_all_low": 10.0, "weekly_all_high": 14.8},
    ]
    assert verdict(rows).endswith("ranges overlap; no change detectable.")


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

    _record(store, _reset("seven_day", MON_B - 60, 100.0, 12.0, 11.5, 13.0, 11, 8.3))
    _record(store, _reset("seven_day", MON_C - 60, 99.0, 9.0, 7.1, 11.1, 13, 11.1))
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"

    with TestClient(app) as tc:
        api = tc.get("/api/weeks").json()
        export = tc.get("/api/export.json?table=weeks").json()
        rows_csv = list(csv.DictReader(io.StringIO(tc.get("/api/export.csv?table=weeks").text)))

    assert [r["week"] for r in api["weeks"]] == [week_key(MON_C - 60), week_key(MON_B - 60)]
    assert "ranges do not overlap" in api["verdict"]
    # The export streams oldest-first; the API is newest-first. Same rows either way.
    assert [r["closed_at"] for r in export["rows"]] == [str(MON_B - 60), str(MON_C - 60)] or [
        int(r["closed_at"]) for r in export["rows"]
    ] == [MON_B - 60, MON_C - 60]
    assert len(rows_csv) == 2 and rows_csv[0]["weekly_all_cost"] in ("12.0", "12")


# -- rendering the section --------------------------------------------------------


def _render(rows, verdict_text: str):
    from quotalens.dashboard import Dashboard, WeekRowView, WeeksView
    from quotalens.render import _weeks

    dash = object.__new__(Dashboard)
    dash.weeks = WeeksView(
        [WeekRowView(*r) for r in rows], verdict_text, "note text"
    )
    return _weeks(dash)


def test_the_section_renders_with_two_complete_weeks() -> None:
    open_row = ("21 Sep - 28 Sep · this week", "collecting", "collecting", "n=2",
                "collecting", "n=1", "12%", "88%", "8.1", True)
    week_c = ("14 Sep - 21 Sep", "21 Sep 01:00", "9", "7-11 · n=13",
              "9", "4-14 · n=13", "100%", "0%", "11.1", False)
    week_b = ("7 Sep - 14 Sep", "14 Sep 01:00", "12", "10-14 · n=17",
              "11", "5-16 · n=16", "99%", "1%", "7.8", False)
    verdict_text = "Weekly cost per session: 9 vs 12 last week; ranges do not overlap."
    html = _render([open_row, week_c, week_b], verdict_text)

    assert "Weeks — one row per weekly reset" in html
    assert "collecting" in html and "ranges do not overlap" in html
    assert html.count("<tr class") == 3  # three body rows; the header <tr> has no class
    assert "Full sessions at reset" in html and "7-11 · n=13" in html


def test_the_section_with_only_the_open_week_shows_collecting_and_says_insufficient() -> None:
    open_row = ("21 Sep - 28 Sep · this week", "collecting", "collecting", "n=0",
                "collecting", "", "—", "—", "—", True)
    html = _render([open_row], "fewer than two complete weeks")
    assert "collecting" in html
    assert "fewer than two complete weeks" in html


def test_an_empty_ledger_renders_nothing() -> None:
    from quotalens.dashboard import Dashboard
    from quotalens.render import _weeks

    dash = object.__new__(Dashboard)
    dash.weeks = None
    assert _weeks(dash) == ""
