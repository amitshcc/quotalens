"""A quota boost: the level fell without the window resetting.

The real one, on 5 Sep 2026, is the evidence these tests are built from. Anthropic
raised this account's weekly limit mid-window and QuotaLens showed nothing:

    before  seven_day 98.0%  resets_at 2026-09-07T00:59:59.875080+00:00
    after   seven_day  0.0%  resets_at 2026-09-07T01:00:00.703741+00:00

0.83 seconds apart — the server's ordinary sub-second jitter, far inside the
60-second tolerance. Same window, count gone.

The hard part is not spotting the fall, it is refusing the five other things that
also make a number fall. One test each, and only the boost is one.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from quotalens.boost import BOOST_KIND, BOOST_MIN_DROP_PCT, detect_boost, detect_boosts
from quotalens.budget import WeeklyLimit, compute_budget, window_costs
from quotalens.parse import QuotaReading
from quotalens.runway import SESSION_LENGTH_S
from quotalens.sessions import Delta, SessionWindow
from quotalens.store import QuotaRow

NOW = 1_800_000_000
WEEK_RESET = "2026-09-07T00:59:59.875080+00:00"
WEEK_RESET_JITTERED = "2026-09-07T01:00:00.703741+00:00"  # 0.83s later: the same window


def row(pct: float, reset: str | None = WEEK_RESET, ts: int = NOW - 60) -> QuotaRow:
    return QuotaRow(ts, "seven_day", "Weekly — all models", pct, reset)


def reading(pct: float, reset: str | None = WEEK_RESET_JITTERED) -> QuotaReading:
    return QuotaReading("seven_day", "Weekly — all models", pct, reset)


# -- the one that is a boost ------------------------------------------------------


def test_the_real_transition_is_detected() -> None:
    """98% to 0% with the reset time 0.83 seconds apart. Replayed from the database."""
    boost = detect_boost(row(98.0), reading(0.0), NOW)

    assert boost is not None
    assert (boost.from_pct, boost.to_pct, boost.drop) == (98.0, 0.0, 98.0)
    assert boost.window == "seven_day" and boost.ts == NOW
    assert boost.detail() == ("Weekly — all models fell 98% -> 0% with no reset. Limit raised.")


def test_a_gap_in_collection_does_not_disqualify_it() -> None:
    """The real boost was seen across six and a half hours of downtime."""
    stale_row = row(98.0, ts=NOW - 23669)
    assert detect_boost(stale_row, reading(0.0), NOW) is not None


# -- the five that are not --------------------------------------------------------


def test_a_genuine_reset_is_not_a_boost() -> None:
    """resets_at moved a week forward: the window ended, which is already handled."""
    next_week = "2026-09-14T01:00:00.000000+00:00"
    assert detect_boost(row(98.0), reading(0.0, next_week), NOW) is None


def test_no_previous_reading_is_not_a_boost() -> None:
    """A withheld or stale collector produces no new value, so there is no fall."""
    assert detect_boost(None, reading(0.0), NOW) is None


@pytest.mark.parametrize("undated", ["previous", "current"])
def test_an_undated_block_on_either_side_is_not_a_boost(undated: str) -> None:
    """Without a reset time there is no way to say the window did *not* reset."""
    before = row(98.0, None if undated == "previous" else WEEK_RESET)
    after = reading(0.0, None if undated == "current" else WEEK_RESET_JITTERED)
    assert detect_boost(before, after, NOW) is None


def test_an_unverified_reading_never_becomes_a_boost() -> None:
    """Recovered by the generic tree walk: not trusted enough to make a claim from."""
    assert detect_boost(row(98.0), reading(0.0), NOW, trusted=False) is None


def test_a_fall_inside_the_noise_floor_is_not_a_boost() -> None:
    """five_hour went 42 to 41 inside one window: the server revising its own number."""
    assert detect_boost(row(42.0), reading(41.0), NOW) is None
    just_under = detect_boost(row(42.0), reading(42.0 - BOOST_MIN_DROP_PCT + 0.1), NOW)
    assert just_under is None
    assert detect_boost(row(42.0), reading(42.0 - BOOST_MIN_DROP_PCT), NOW) is not None


def test_a_rise_is_not_a_boost() -> None:
    assert detect_boost(row(40.0), reading(60.0), NOW) is None


def test_detect_boosts_reports_every_window_that_moved() -> None:
    previous = [row(98.0), QuotaRow(NOW - 60, "limit:fable", "Fable", 100.0, WEEK_RESET)]
    readings = [
        reading(0.0),
        QuotaReading("limit:fable", "Fable", 1.0, WEEK_RESET_JITTERED),
        QuotaReading("five_hour", "5-hour", 22.0, "2026-09-05T07:30:00+00:00"),
    ]
    found = detect_boosts(previous, readings, NOW)
    assert [b.window for b in found] == ["seven_day", "limit:fable"]


# -- the part that silently corrupts a number -------------------------------------


def _window(index: int, peak: float, points: float) -> SessionWindow:
    start = NOW - (index + 1) * SESSION_LENGTH_S
    return SessionWindow(
        start,
        start + SESSION_LENGTH_S,
        False,
        peak,
        peak,
        300,
        start,
        start + SESSION_LENGTH_S,
        {"seven_day": Delta(10.0, 10.0 + points, False)},
        SESSION_LENGTH_S,
    )


def test_a_boosted_window_is_left_out_of_the_cost_estimate() -> None:
    """Its weekly delta is consumption minus a raise, and the raise has no size."""
    history = [_window(i, 100.0, 10.0) for i in range(5)]
    boosted = _window(5, 100.0, 1.0)  # a full session that "cost" almost nothing
    inside = boosted.started_at + 600

    assert len(window_costs([*history, boosted], "seven_day", NOW)) == 6
    assert len(window_costs([*history, boosted], "seven_day", NOW, [inside])) == 5


def test_the_median_cost_is_unchanged_by_the_boost() -> None:
    """The whole point: a boost must not make the budget quietly optimistic."""
    history = [_window(i, 100.0, 10.0) for i in range(5)]
    boosted = _window(5, 100.0, 1.0)
    inside = boosted.started_at + 600
    limit = WeeklyLimit("seven_day", "Weekly", 50.0, NOW + 86400, False)

    clean = compute_budget(limit, history, NOW)
    guarded = compute_budget(limit, [*history, boosted], NOW, [inside])
    unguarded = compute_budget(limit, [*history, boosted], NOW)

    assert guarded.cost_per_full == clean.cost_per_full == pytest.approx(10.0)
    assert guarded.full_windows == clean.full_windows
    assert guarded.usable == 5 and guarded.cost_low == pytest.approx(10.0)
    # One boosted window does not move a median — that is what a median is for — but
    # it lands in the sample and drags the spread, which is what the reader is shown.
    assert unguarded.usable == 6 and unguarded.cost_low == pytest.approx(1.0)


def test_several_boosted_windows_would_move_the_median_itself() -> None:
    """The robustness above is not a reason to let them in: it runs out."""
    history = [_window(i, 100.0, 10.0) for i in range(5)]
    boosted = [_window(5 + i, 100.0, 1.0) for i in range(6)]  # enough to outvote
    inside = [w.started_at + 600 for w in boosted]
    limit = WeeklyLimit("seven_day", "Weekly", 50.0, NOW + 86400, False)

    guarded = compute_budget(limit, [*history, *boosted], NOW, inside)
    unguarded = compute_budget(limit, [*history, *boosted], NOW)

    assert guarded.cost_per_full == pytest.approx(10.0)
    assert unguarded.cost_per_full < guarded.cost_per_full
    # And the budget it produces is optimistic: more sessions than there are.
    assert unguarded.full_windows > guarded.full_windows


def test_a_boost_outside_a_window_does_not_exclude_it() -> None:
    history = [_window(i, 100.0, 10.0) for i in range(5)]
    long_before = history[-1].started_at - 3600
    assert len(window_costs(history, "seven_day", NOW, [long_before])) == 5


# -- end to end -------------------------------------------------------------------


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def test_a_boost_records_an_event_and_no_alert_fires(settings, store, secrets) -> None:
    """A boost collapses the burn rate; that is not a recovery anyone earned."""
    from fastapi.testclient import TestClient

    from quotalens.alerts import CLEARED_KIND
    from quotalens.api import create_app

    now = int(time.time())
    reset = iso(now + 3 * 3600)
    for i in range(20):  # a steep climb to 90%, well over the alert threshold
        store.record_quota(
            now - (20 - i) * 60,
            [QuotaReading("five_hour", "5-hour", 20.0 + i * 3.5, reset, None, True)],
        )
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    poller = app.state.qw.poller
    poller._detector.firing = True  # the alert is already standing

    previous = store.latest_quota()
    boosted = [QuotaReading("five_hour", "5-hour", 2.0, iso(now + 3 * 3600 + 0.8), None, True)]
    store.record_quota(now, boosted)

    from quotalens.parse import UsageParse

    poller._check_boost(now, previous, UsageParse(boosted))
    poller._check_threshold(now, UsageParse(boosted))

    kinds = [e.kind for e in store.recent_events(limit=20)]
    assert BOOST_KIND in kinds
    assert CLEARED_KIND not in kinds, "a raised limit is not a recovery"
    assert poller._detector.firing is False, "the detector still clears for the next crossing"

    with TestClient(app) as client:
        events = client.get("/api/events").json()
    assert any(e["kind"] == BOOST_KIND for e in events["events"])
