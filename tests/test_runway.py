"""Runway: projection, sustainable rate, verdicts, comparison, and the hour strip."""

from __future__ import annotations

from quotalens.runway import (
    compute_runway,
    hour_strip,
    median_peak,
    project,
    sustainable_rate,
)
from quotalens.store import QuotaRow

NOW = 1_800_000_000


def row(ts: int, pct: float, reset: str = "r1") -> QuotaRow:
    return QuotaRow(ts, "five_hour", "5-hour", pct, reset)


def test_projection_crossing_before_reset_and_not_crossing() -> None:
    reset = NOW + 2 * 3600
    exhaust, finish = project(60, 30, NOW, reset)  # 40 points left at 30/hr: 80 minutes
    assert exhaust == NOW + 4800 and finish == 100.0
    exhaust, finish = project(60, 10, NOW, reset)  # +20 in 2h: survives
    assert exhaust is None and finish == 80.0
    assert project(60, None, NOW, reset) == (None, None)
    assert project(60, 30, NOW, NOW - 1) == (None, None)  # reset already passed


def test_sustainable_rate_at_zero_headroom_and_zero_time() -> None:
    assert sustainable_rate(40, 2 * 3600) == 20.0
    assert sustainable_rate(0, 2 * 3600) == 0.0
    assert sustainable_rate(40, 0) is None
    assert sustainable_rate(None, 3600) is None


def test_verdicts() -> None:
    reset = NOW + 2 * 3600 + 14 * 60
    burning = compute_runway(63, 30, 15 * 60, reset, NOW)
    assert burning.verdict.startswith("Exhausted at ") and "before reset." in burning.verdict
    assert burning.critical and burning.headroom_pct == 37 and burning.remaining_s == 8040
    assert burning.sustainable == round(37 / (8040 / 3600), 2)
    surviving = compute_runway(63, 5, 15 * 60, reset, NOW)
    assert surviving.verdict == "At this rate you finish with 26% unused."
    assert not surviving.critical
    idle = compute_runway(63, 0.0, 4 * 60, reset, NOW)
    assert idle.verdict == "Flat for 4m. 37% left, resets in 2h 14m."
    collecting = compute_runway(63, None, 3 * 60, reset, NOW)
    assert collecting.verdict == "Collecting: 3m of samples. 37% left, resets in 2h 14m."
    none = compute_runway(63, 30, 900, None, NOW)
    assert none.verdict.startswith("No session running") and none.remaining_s == 0
    assert compute_runway(None, None, 0, reset, NOW).verdict == "No session readings yet."


def test_countdown_at_the_moment_of_reset() -> None:
    """A window whose expiry has arrived is closed, and its 63% is no longer current.

    It used to pass the last percentage straight through this branch, so the page
    showed a headroom figure for a window that no longer existed.
    """
    at_reset = compute_runway(63, 30, 900, NOW, NOW)
    assert at_reset.remaining_s == 0 and at_reset.sustainable is None
    assert at_reset.pct is None and at_reset.headroom_pct is None
    assert at_reset.verdict.startswith("The session window ended at")
    one_second = compute_runway(99.5, 3600, 900, NOW + 1, NOW)  # one point per second
    assert one_second.remaining_s == 1 and one_second.verdict.startswith("Exhausted at")


def test_median_comparison_suppressed_under_five_windows() -> None:
    assert median_peak([40, 50, 60, 70]) is None
    assert median_peak([40, 50, 60, 70, 80]) == 60
    reset = NOW + 3600
    with_baseline = compute_runway(60, 12, 900, reset, NOW, baseline=40.0)
    assert with_baseline.comparison == "On track for 1.8× your median window (40%)."
    assert compute_runway(60, 12, 900, reset, NOW, baseline=None).comparison == ""
    flat = compute_runway(60, 0.0, 900, reset, NOW, baseline=40.0)
    assert flat.comparison == "On track for 1.5× your median window (40%)."


def test_hour_strip_partial_window() -> None:
    start = NOW - 2 * 3600 - 1800  # two and a half hours in
    rows = [row(start + m * 60, m / 6) for m in range(0, 151, 5)]  # +10 pts per hour
    bars = hour_strip(rows, start, NOW)
    assert [b.state for b in bars] == ["done", "done", "partial", "future", "future"]
    assert bars[0].consumed == 10.0 and bars[1].consumed == 10.0
    assert bars[2].consumed == 5.0  # half an hour into the third
    assert bars[3].consumed is None and bars[4].consumed is None
    on_the_hour = hour_strip(rows, NOW - 3 * 3600, NOW)  # the fourth hour starts this second
    assert [b.state for b in on_the_hour][2:] == ["done", "future", "future"]


def test_hour_strip_spanning_a_reset_and_no_data() -> None:
    start = NOW - 5 * 3600
    rows = [row(start + m * 60, 80 + m / 10, "r1") for m in range(0, 30, 5)]
    rows += [row(start + m * 60, (m - 30) / 10, "r2") for m in range(30, 61, 5)]
    bars = hour_strip(rows, start, NOW)
    assert bars[0].consumed == 5.5  # 2.5 before the reset plus 3.0 after, never negative
    assert bars[1].state == "nodata" and bars[1].consumed is None
