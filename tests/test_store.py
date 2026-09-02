from __future__ import annotations

import sqlite3

from quotawatch.parse import OverageReading, QuotaReading
from quotawatch.store import SCHEMA_VERSION, Store


def test_schema_created_and_versioned(store: Store) -> None:
    conn = sqlite3.connect(store.path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "sample",
        "quota",
        "overage",
        "local_turn",
        "scan_state",
        "event",
        "schema_version",
    } <= tables
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_reopen_does_not_duplicate_version_rows(settings) -> None:
    Store(settings.db_path).close()
    s = Store(settings.db_path)
    conn = sqlite3.connect(s.path)
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    conn.close()
    s.close()


def test_quota_roundtrip_latest_and_series(store: Store) -> None:
    store.record_quota(
        100,
        [
            QuotaReading("five_hour", "5-hour", 10, "r1"),
            QuotaReading("seven_day", "7-day", 1, "r2"),
        ],
    )
    store.record_quota(160, [QuotaReading("five_hour", "5-hour", 12, "r1")])
    latest = {r.window: r for r in store.latest_quota()}
    assert latest["five_hour"].pct == 12 and latest["five_hour"].ts == 160
    assert latest["seven_day"].ts == 100
    series = store.quota_series(since_ts=0, window="five_hour")
    assert [r.pct for r in series] == [10, 12]
    assert store.quota_series(since_ts=150) == [latest["five_hour"]]
    assert store.windows() == ["five_hour", "seven_day"]


def test_sample_overage_events_and_counts(store: Store) -> None:
    store.record_sample(1, "usage", {"a": 1})
    store.record_overage(1, OverageReading(100, 500, "USD"))
    store.record_event("poll_error", "boom", ts=5)
    store.record_event("auth_expired", "401", ts=6)
    assert store.latest_overage() == {
        "ts": 1,
        "spent_minor": 100,
        "cap_minor": 500,
        "currency": "USD",
    }
    assert [e.kind for e in store.recent_events()] == ["auth_expired", "poll_error"]
    assert [e.kind for e in store.recent_events(kind="poll_error")] == ["poll_error"]
    assert store.counts() == {"quota": 0, "sample": 1, "overage": 1, "event": 2}


def test_memory_store() -> None:
    s = Store(":memory:")
    s.record_quota(1, [QuotaReading("w", "w", 1, None)])
    assert s.counts()["quota"] == 1
    s.close()
