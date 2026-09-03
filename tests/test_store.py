from __future__ import annotations

import sqlite3

from quotalens.parse import QuotaReading, SpendReading
from quotalens.store import SCHEMA_VERSION, Store


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
    store.record_overage(1, SpendReading(100, 500, 2, "USD", "spend"))
    store.record_event("poll_error", "boom", ts=5)
    store.record_event("auth_expired", "401", ts=6)
    assert store.latest_overage() == {
        "ts": 1,
        "spent_minor": 100,
        "cap_minor": 500,
        "currency": "USD",
        "exponent": 2,
    }
    assert [e.kind for e in store.recent_events()] == ["auth_expired", "poll_error"]
    assert [e.kind for e in store.recent_events(kind="poll_error")] == ["poll_error"]
    assert store.counts() == {
        "quota": 0,
        "sample": 1,
        "overage": 1,
        "event": 2,
        "session_window": 0,
    }


def test_memory_store() -> None:
    s = Store(":memory:")
    s.record_quota(1, [QuotaReading("w", "w", 1, None)])
    assert s.counts()["quota"] == 1
    s.close()


V1_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at INTEGER NOT NULL);
CREATE TABLE sample (ts INTEGER NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE quota (ts INTEGER NOT NULL, window TEXT NOT NULL, label TEXT NOT NULL,
    pct REAL NOT NULL, resets_at TEXT, PRIMARY KEY (ts, window));
CREATE TABLE overage (ts INTEGER PRIMARY KEY, spent_minor INTEGER NOT NULL,
    cap_minor INTEGER NOT NULL, currency TEXT NOT NULL);
CREATE TABLE event (ts INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
INSERT INTO schema_version VALUES (1, 0);
INSERT INTO quota VALUES (10, 'five_hour', '5-hour', 42.0, 'r1');
INSERT INTO overage VALUES (10, 316, 200, 'USD');
"""


def test_v1_database_is_migrated_without_losing_rows(tmp_path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.close()

    store = Store(path)
    rows = store.latest_quota()
    assert rows[0].pct == 42.0 and rows[0].severity is None and rows[0].is_active is None
    assert store.latest_overage()["exponent"] == 2
    store.record_quota(20, [QuotaReading("five_hour", "5-hour", 43, "r1", "warning", True)])
    latest = store.latest_quota()[0]
    assert (latest.severity, latest.is_active) == ("warning", True)
    conn = sqlite3.connect(path)
    assert [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")] == [
        1,
        2,
        3,
        4,
        5,
    ]
    conn.close()
    store.close()
