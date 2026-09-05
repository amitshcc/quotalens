"""Retention: the tiering, the measured estimates, and the two destructive edges."""

from __future__ import annotations

import time

import pytest

from quotalens import retention as R
from quotalens.sessions import rebuild
from quotalens.store import Store

DAY = 86_400


SESSION_S = 5 * 3600


def _seed(store: Store, now: int, days: int) -> None:
    """One reading per hour, with a five-hour reset boundary, so windows derive."""
    from datetime import UTC, datetime

    from quotalens.parse import QuotaReading

    for h in range(days * 24):
        ts = now - h * 3600
        ends = (ts // SESSION_S + 1) * SESSION_S
        store.record_quota(
            ts,
            [
                QuotaReading(
                    window="five_hour",
                    label="Session",
                    pct=50.0,
                    resets_at=datetime.fromtimestamp(ends, UTC).isoformat(),
                    severity=None,
                    is_active=True,
                )
            ],
        )


def test_the_floor_tables_are_not_governed_by_the_dropdown() -> None:
    cuts = R.cutoffs("1week", 1_000_000_000)
    assert cuts["quota"] == 1_000_000_000 - 7 * DAY
    for table in R.KEPT_TABLES:
        assert cuts[table] == 1_000_000_000 - R.FLOOR_DAYS * DAY


def test_no_option_keeps_data_forever() -> None:
    """Asked for explicitly: there is no indefinite choice."""
    assert all(o.days <= 365 for o in R.RETENTION_OPTIONS)
    assert len(R.RETENTION_OPTIONS) == 5


def test_too_little_history_gives_an_em_dash_not_a_guess() -> None:
    m = R.measure(span_s=1.5 * DAY, rows={"quota": 100}, raw_row_bytes={"quota": 80}, total_bytes=9)
    estimates = R.estimate(m, sample_keep=20_000)
    assert [e.bytes for e in estimates] == [None] * 5
    assert all(e.reason == "needs more history" for e in estimates)
    assert R.format_bytes(None) == "—"


def test_estimates_grow_with_the_period_and_are_calibrated_to_the_real_size() -> None:
    rows = {"quota": 7_200, "sample": 1_000, "overage": 1_440, "event": 5, "session_window": 5}
    widths = {"quota": 100.0, "sample": 3000.0, "overage": 30.0, "event": 140.0}
    m = R.measure(span_s=1 * DAY, rows=rows, raw_row_bytes=widths, total_bytes=5_000_000)
    # One day of rows should model to about the real file size, not the raw sum.
    modelled = sum(m.row_bytes.get(t, 0) * rows[t] for t in rows)
    assert modelled == pytest.approx(5_000_000, rel=0.01)

    m = R.measure(span_s=3 * DAY, rows=rows, raw_row_bytes=widths, total_bytes=5_000_000)
    sizes = [e.bytes for e in R.estimate(m, sample_keep=20_000)]
    assert all(a < b for a, b in zip(sizes, sizes[1:], strict=False))


def test_the_sample_cap_bounds_the_projection() -> None:
    """`sample` stops growing at its row cap, so the estimate must stop too."""
    rows = {"quota": 100, "sample": 100_000, "overage": 10, "event": 1, "session_window": 1}
    widths = dict.fromkeys(rows, 100.0)
    m = R.measure(span_s=3 * DAY, rows=rows, raw_row_bytes=widths, total_bytes=1_000_000)
    small = R.estimate(m, sample_keep=100)
    large = R.estimate(m, sample_keep=100_000)
    assert small[-1].bytes < large[-1].bytes


def test_an_existing_database_gets_the_longest_option_not_the_default() -> None:
    """The first run after upgrade must not delete history nobody agreed to lose."""
    assert R.initial_retention(45.0) == "1year"
    assert R.initial_retention(None) == R.DEFAULT_RETENTION == "3months"
    assert R.initial_retention(0) == "3months"


def test_initialise_writes_once_and_says_so(tmp_path) -> None:
    written: dict = {}
    events: list = []
    value, notice = R.initialise(
        stored={},
        history_days=40.0,
        write=written.update,
        record_event=lambda kind, detail, ts: events.append((kind, detail)),
        now=1_000,
    )
    assert value == "1year" and written == {"retention": "1year"}
    assert "nothing you already have was deleted" in notice
    assert events[0][0] == R.RETENTION_SET_KIND

    # Second call: the stored value stands, nothing is rewritten, no second notice.
    again, notice2 = R.initialise(
        stored={"retention": "1week"},
        history_days=40.0,
        write=lambda _d: pytest.fail("must not rewrite an existing choice"),
        record_event=lambda *_a, **_k: pytest.fail("must not re-announce"),
        now=1_000,
    )
    assert again == "1week" and notice2 is None


def test_pruning_quota_does_not_destroy_the_session_windows_derived_from_it(store) -> None:
    """The bug this feature nearly shipped with.

    Retention keeps `session_window` for two years, but the startup rebuild
    re-derives the whole table from `quota`. Prune `quota` to a week, restart,
    and every older window silently disappeared -- observed as 11 rows becoming
    7 -- taking the budget table's baseline with it.
    """
    now = int(time.time())
    _seed(store, now, days=6)
    assert rebuild(store, now, keep_underivable=True) >= 1
    before = store.counts()["session_window"]
    assert before >= 2

    store.prune_by_age(R.cutoffs("1week", now + 5 * DAY))  # leaves only the last day
    assert store.counts()["quota"] < 6 * 24

    rebuild(store, now, keep_underivable=True)
    assert store.counts()["session_window"] == before


def test_forget_still_removes_the_window_it_deliberately_deleted(store) -> None:
    """The opposite case: an explicit deletion must not be preserved."""
    now = int(time.time())
    _seed(store, now, days=6)
    rebuild(store, now, keep_underivable=True)
    before = store.counts()["session_window"]

    store.prune_by_age(R.cutoffs("1week", now + 5 * DAY))
    assert rebuild(store, now, keep_underivable=False) < before
