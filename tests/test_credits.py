"""Usage credits: detecting *when* money was spent, from the stored series."""

from __future__ import annotations

from quotalens import credits
from quotalens.credits import OverageRow


def _rows(*pairs: tuple[int, int]) -> list[OverageRow]:
    return [OverageRow(ts, spent, 3000, "USD", 2) for ts, spent in pairs]


def test_spending_is_a_rise_between_two_polls() -> None:
    rows = _rows((0, 100), (60, 100), (120, 250), (180, 400), (240, 400), (300, 400), (360, 400))
    runs = credits.stretches(rows, 60)
    assert len(runs) == 1
    assert runs[0].from_minor == 100 and runs[0].to_minor == 400
    assert runs[0].amount_minor == 300


def test_a_flat_series_is_not_spending() -> None:
    """An exhausted window is not money moving; only a rise is."""
    assert credits.stretches(_rows((0, 500), (60, 500), (120, 500)), 60) == []


def test_a_pause_shorter_than_the_quiet_window_is_one_stretch() -> None:
    """One quiet poll is a gap between requests, not the end of a session."""
    rows = _rows((0, 0), (60, 10), (120, 10), (180, 25), (240, 25), (300, 25), (360, 25))
    runs = credits.stretches(rows, 60)
    assert len(runs) == 1 and runs[0].amount_minor == 25


def test_a_long_quiet_stretch_ends_it_and_a_new_rise_starts_another() -> None:
    rows = _rows(
        (0, 0),
        (60, 10),
        (120, 10),
        (180, 10),
        (240, 10),
        (300, 10),
        (360, 50),
        (420, 50),
        (480, 50),
        (540, 50),
    )
    runs = credits.stretches(rows, 60)
    assert len(runs) == 2
    assert [r.amount_minor for r in runs] == [10, 40]


def test_a_rise_across_a_collection_gap_is_marked_approximate() -> None:
    """The money moved somewhere in the gap, not at the instant we noticed."""
    rows = _rows((0, 0), (6000, 900), (6060, 900), (6120, 900), (6180, 900))
    runs = credits.stretches(rows, 60)
    assert len(runs) == 1 and runs[0].approximate
    assert "collection gap" in runs[0].detail()

    tight = credits.stretches(_rows((0, 0), (60, 900), (120, 900), (180, 900), (240, 900)), 60)
    assert not tight[0].approximate


def test_spent_in_range_is_a_difference_not_a_sum() -> None:
    """A counter that resets at the start of a month must not double-count."""
    rows = _rows((0, 100), (60, 200), (120, 350))
    assert credits.spent_in_range(rows, 0, 120) == 250
    assert credits.spent_in_range(rows, 60, 120) == 150
    assert credits.spent_in_range(rows, 0, 0) == 0  # one row is not a difference


def test_seconds_on_credits_clips_to_the_range() -> None:
    rows = _rows((0, 0), (60, 10), (120, 20), (180, 20), (240, 20), (300, 20))
    runs = credits.stretches(rows, 60)
    assert credits.seconds_on_credits(runs, 0, 1000) == 120
    assert credits.seconds_on_credits(runs, 60, 90) == 30
    assert credits.seconds_on_credits(runs, 500, 900) == 0


def test_backfill_skips_stretches_already_recorded() -> None:
    """History on disk lights up once, not on every poll."""
    rows = _rows((0, 0), (60, 10), (120, 10), (180, 10), (240, 10))
    runs = credits.stretches(rows, 60)
    assert credits.backfill(rows, 60, set()) == runs
    assert credits.backfill(rows, 60, {runs[0].start_ts}) == []


def test_recorded_starts_reads_the_event_timestamps() -> None:
    """The event is written at the stretch's start, so nothing is parsed from prose."""

    class Row:
        def __init__(self, ts):
            self.ts = ts

    assert credits.recorded_starts([Row(10), Row(20)]) == {10, 20}


def test_the_event_line_carries_no_time_and_the_notification_does() -> None:
    """The events list prefixes its own time; a banner has nothing around it."""
    run = credits.stretches(_rows((0, 0), (60, 500), (120, 500), (180, 500), (240, 500)), 60)[0]
    assert run.detail() == "credits started · $5.00 so far"
    assert run.notification(lambda _t: "01:02") == "Credits started 01:02 · $5.00 so far"
