"""The continuous check that Fable's 100% really is half of the weekly pool.

Two invariants from the help centre's rule, tested in both directions: they must pass on
the shapes the current database shows and fail the moment Fable exceeds its half.
"""

from __future__ import annotations

from quotalens.subcap import (
    SUBCAP_KIND,
    any_recorded,
    check,
    latest_detail,
    reading_ok,
    reading_violation,
    window_ok,
    window_violation,
)

NOW = 1_800_000_000


# -- invariant 1: fable_pct / 2 <= all_pct + 1 ------------------------------------


def test_reading_invariant_holds_for_the_shapes_in_the_database() -> None:
    # Checked by hand in the prompt: 6/2 ≤ 8, 85/2 ≤ 100, 82/2 ≤ 67.
    assert reading_ok(8.0, 6.0)
    assert reading_ok(100.0, 85.0)
    assert reading_ok(67.0, 82.0)
    assert reading_violation(67.0, 82.0) is None


def test_reading_invariant_fails_when_fable_exceeds_twice_all() -> None:
    assert not reading_ok(3.0, 10.0)  # 5 > 4
    assert reading_violation(3.0, 10.0) is not None
    assert "does not hold" in reading_violation(3.0, 10.0)


def test_reading_invariant_allows_one_point_of_rounding() -> None:
    assert reading_ok(3.0, 8.0)  # 4.0 <= 4.0, exactly the slack
    assert not reading_ok(3.0, 8.2)  # 4.1 > 4.0


def test_reading_violation_is_silent_on_a_missing_reading() -> None:
    assert reading_violation(None, 50.0) is None
    assert reading_violation(50.0, None) is None


# -- invariant 2: delta_fable <= 2 * delta_all + 1 --------------------------------


def test_window_invariant_holds_and_fails_both_ways() -> None:
    assert window_ok(10.0, 20.0)  # 20 <= 21
    assert window_ok(10.0, 21.0)  # 21 <= 21, the slack boundary
    assert not window_ok(5.0, 20.0)  # 20 > 11
    assert window_violation(5.0, 20.0) is not None
    assert window_violation(10.0, 20.0) is None


def test_window_violation_is_silent_on_a_missing_delta() -> None:
    assert window_violation(None, 5.0) is None
    assert window_violation(5.0, None) is None


# -- the poll-path recorder -------------------------------------------------------


def test_a_violation_is_recorded_once_per_window(settings, store) -> None:
    window_start = NOW - 3600
    msgs = check(store, 3.0, 10.0, None, None, window_start, NOW)
    assert msgs and any_recorded(store)
    assert latest_detail(store) is not None

    # A second poll inside the same window must not write a second event.
    check(store, 3.0, 10.0, None, None, window_start, NOW + 60)
    assert len(store.recent_events(limit=10, kind=SUBCAP_KIND)) == 1


def test_a_new_window_the_next_day_can_record_again(settings, store) -> None:
    check(store, 3.0, 10.0, None, None, NOW - 3600, NOW)
    later = NOW + 2 * 86400
    check(store, 3.0, 10.0, None, None, later - 3600, later)
    assert len(store.recent_events(limit=10, kind=SUBCAP_KIND)) == 2


def test_nothing_is_recorded_when_both_invariants_hold(settings, store) -> None:
    assert check(store, 8.0, 6.0, 10.0, 5.0, NOW - 3600, NOW) == []
    assert not any_recorded(store)


def test_both_invariants_report_together(settings, store) -> None:
    msgs = check(store, 3.0, 10.0, 5.0, 20.0, NOW - 3600, NOW)
    assert len(msgs) == 2
    detail = latest_detail(store)
    assert "Fable at 10%" in detail and "Over one session" in detail
