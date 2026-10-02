from __future__ import annotations

import sqlite3

from quotalens.parse import (
    CreditGrant,
    QuotaReading,
    SpendReading,
    SurfaceBreakdown,
    SurfaceShare,
)
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
        6,
        7,
        8,
        9,
        10,
        11,
    ]
    conn.close()
    store.close()


def test_event_ids_survive_the_migration_and_vacuum(tmp_path) -> None:
    """The /api/events cursor is the event id: v10 keeps the old rowids and VACUUM keeps ids."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    for ts in (30, 10, 20):
        conn.execute("INSERT INTO event VALUES (?, 'k', ?)", (ts, f"d{ts}"))
    conn.execute("DELETE FROM event WHERE ts = 10")  # a hole, as a prune leaves
    conn.commit()
    before = conn.execute("SELECT rowid, ts FROM event ORDER BY rowid").fetchall()
    conn.close()

    store = Store(path)
    assert [(e.id, e.ts) for e in store.events_after(0)] == before == [(1, 30), (3, 20)]
    store.record_event("k", "new", ts=5)
    store.vacuum()
    assert [(e.id, e.ts) for e in store.events_after(0)] == [(1, 30), (3, 20), (4, 5)]
    store.delete_events("k", [5])
    store.record_event("k", "again", ts=6)
    assert store.events_after(3)[0].id == 5  # an id is never reused
    store.close()


def _grant(ts_used: int = 2129, **over) -> CreditGrant:
    fields = dict(
        key="iguana_necktie",
        label="Cloud session credit",
        used_minor=ts_used,
        limit_minor=25000,
        remaining_minor=25000 - ts_used,
        pct=8.5,
        expires_at="2026-11-05T07:59:00+00:00",
        locked_reason=None,
    )
    return CreditGrant(**{**fields, **over})


def test_schema_v6_migration_removes_grant_quota_rows(tmp_path) -> None:
    path = tmp_path / "v5.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO quota VALUES (11, 'iguana_necktie', 'iguana necktie', 8.5, 'r')")
    conn.execute("INSERT INTO quota VALUES (11, 'seven_day', 'Weekly', 30, 'r')")
    conn.commit()
    conn.close()

    store = Store(path)
    assert {r.window for r in store.latest_quota()} == {"five_hour", "seven_day"}
    assert store.latest_grants() == []
    store.close()


def test_record_and_latest_grants(store: Store) -> None:
    assert store.latest_grants() == []
    store.record_grants(10, [_grant(1000)])
    store.record_grants(20, [_grant(2129), _grant(5, key="other", label="other (unrecognised)")])
    store.record_grants(20, [_grant(2129)])  # the same (ts, key) is replaced, not duplicated
    latest = {g.key: g for g in store.latest_grants()}
    assert set(latest) == {"iguana_necktie", "other"}
    assert latest["iguana_necktie"].ts == 20
    assert latest["iguana_necktie"].used_minor == 2129
    assert latest["iguana_necktie"].remaining_minor == 22871
    assert latest["iguana_necktie"].expires_at == "2026-11-05T07:59:00+00:00"


WEEK = "2026-09-28T01:00:00+00:00"


def _breakdown(code: float, cowork: float, week: str | None = WEEK) -> SurfaceBreakdown:
    return SurfaceBreakdown(
        as_of="2026-09-30T00:00:00+00:00",
        window_started_at=week,
        rows=[
            SurfaceShare("claude_code", "Claude Code", code),
            SurfaceShare("cowork", "Cowork", cowork),
        ],
    )


def test_breakdown_change_only(store: Store) -> None:
    assert store.record_breakdown(10, _breakdown(23, 77)) == 2
    assert store.record_breakdown(20, _breakdown(23, 77)) == 0  # same values: nothing stored
    assert store.record_breakdown(30, _breakdown(24, 76)) == 2
    assert store.record_breakdown(40, _breakdown(10, 90, week="2026-10-05T01:00:00+00:00")) == 2
    assert store.record_breakdown(50, None) == 0
    assert store.query("SELECT COUNT(*) AS n FROM surface_share")[0]["n"] == 6
    latest = store.latest_breakdown()
    assert latest is not None and latest.ts == 40
    assert [(r.key, r.percent) for r in latest.rows] == [("claude_code", 10.0), ("cowork", 90.0)]
    closed = store.breakdown_at_close(WEEK)
    assert closed is not None and closed.ts == 30
    assert [(r.label, r.percent) for r in closed.rows] == [("Claude Code", 24.0), ("Cowork", 76.0)]
    assert store.breakdown_at_close("never") is None


# -- credit_grant: a row on change, plus one heartbeat a day (WP-35) -----------------


def _grant_rows(store: Store) -> list[tuple[int, int]]:
    return [tuple(r) for r in store.query("SELECT ts, used_minor FROM credit_grant ORDER BY ts")]


def test_grant_identical_poll_writes_no_row(store: Store) -> None:
    assert store.record_grants(1_000, [_grant(2129)]) == 1
    assert store.record_grants(1_060, [_grant(2129)]) == 0
    assert store.record_grants(1_120, [_grant(2129)]) == 0
    assert _grant_rows(store) == [(1_000, 2129)]
    assert store.latest_grants()[0].ts == 1_000


def test_grant_change_writes_a_row(store: Store) -> None:
    store.record_grants(1_000, [_grant(2129)])
    store.record_grants(1_060, [_grant(2200)])  # used moved
    store.record_grants(1_120, [_grant(2200, locked_reason="expired")])  # another field
    store.record_grants(1_180, [_grant(2200, key="other")])  # a new key is a change
    assert _grant_rows(store) == [(1_000, 2129), (1_060, 2200), (1_120, 2200), (1_180, 2200)]


def test_grant_heartbeat_once_per_day(store: Store) -> None:
    day = 86_400
    for ts in range(0, 2 * day + 1, 600):  # a poll every 10 min for two days
        store.record_grants(1_000 + ts, [_grant(2129)])
    assert [ts for ts, _ in _grant_rows(store)] == [1_000, 1_000 + day, 1_000 + 2 * day]


def test_grant_compaction_keeps_run_boundaries(tmp_path) -> None:
    """The v11 migration keeps the first row of each run of identical rows per key."""
    path = tmp_path / "v10.db"
    s = Store(path)
    s.close()
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM schema_version WHERE version = 11")
    sql = (
        "INSERT INTO credit_grant (ts, key, label, used_minor, limit_minor, remaining_minor, "
        "expires_at, locked_reason) VALUES (?, ?, 'c', ?, 25000, ?, 'e', ?)"
    )
    for ts, key, used, locked in [
        (10, "g", 100, None),
        (20, "g", 100, None),
        (30, "g", 200, None),
        (40, "g", 200, None),
        (50, "g", 100, None),  # back to an earlier value: a new run
        (60, "g", 100, "x"),  # a nullable field changed
        (70, "g", 100, "x"),
        (15, "h", 100, None),  # another key, interleaved, its own runs
        (25, "h", 100, None),
    ]:
        conn.execute(sql, (ts, key, used, 25000 - used, locked))
    conn.commit()
    conn.close()

    again = Store(path)
    kept = [tuple(r) for r in again.query("SELECT key, ts FROM credit_grant ORDER BY key, ts")]
    assert kept == [("g", 10), ("g", 30), ("g", 50), ("g", 60), ("h", 15)]
    index = again.query("SELECT sql FROM sqlite_master WHERE name = 'credit_grant_key_ts'")
    assert "(key, ts)" in index[0][0]
    again.close()
