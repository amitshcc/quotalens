"""SQLite storage. One file, stdlib ``sqlite3``, no ORM.

Schema follows PLAN.md. Migrations are ``CREATE TABLE IF NOT EXISTS`` plus a
``schema_version`` row; bump :data:`SCHEMA_VERSION` when a table changes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quotawatch.parse import OverageReading, QuotaReading

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at INTEGER NOT NULL
);
-- raw payloads, for debugging endpoint drift
CREATE TABLE IF NOT EXISTS sample (
    ts INTEGER NOT NULL,
    source TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sample_ts ON sample (ts);
-- one row per window per poll
CREATE TABLE IF NOT EXISTS quota (
    ts INTEGER NOT NULL,
    window TEXT NOT NULL,
    label TEXT NOT NULL,
    pct REAL NOT NULL,
    resets_at TEXT,
    PRIMARY KEY (ts, window)
);
CREATE INDEX IF NOT EXISTS quota_window_ts ON quota (window, ts);
CREATE TABLE IF NOT EXISTS overage (
    ts INTEGER PRIMARY KEY,
    spent_minor INTEGER NOT NULL,
    cap_minor INTEGER NOT NULL,
    currency TEXT NOT NULL
);
-- one row per Claude Code turn, from JSONL (populated from M3)
CREATE TABLE IF NOT EXISTS local_turn (
    id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    project TEXT NOT NULL,
    session_id TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read INTEGER,
    cache_creation INTEGER
);
CREATE INDEX IF NOT EXISTS local_turn_project_ts ON local_turn (project, ts);
-- ingestion bookmarks so restarts don't re-scan
CREATE TABLE IF NOT EXISTS scan_state (
    path TEXT PRIMARY KEY,
    offset INTEGER NOT NULL,
    mtime INTEGER NOT NULL
);
-- detected climbs, threshold crossings, poll failures
CREATE TABLE IF NOT EXISTS event (
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_ts ON event (ts);
"""


@dataclass(frozen=True)
class QuotaRow:
    ts: int
    window: str
    label: str
    pct: float
    resets_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "window": self.window,
            "label": self.label,
            "pct": self.pct,
            "resets_at": self.resets_at,
        }


@dataclass(frozen=True)
class EventRow:
    ts: int
    kind: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "kind": self.kind, "detail": self.detail}


def now_ts() -> int:
    return int(time.time())


class Store:
    """Thread-safe wrapper around one SQLite connection.

    The poller (event loop) and the API (threadpool) share it, hence the lock.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if str(path) != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._tx() as cur:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def _migrate(self) -> None:
        with self._tx() as cur:
            cur.executescript(_SCHEMA)
            row = cur.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] if row and row["v"] is not None else 0
            if current < SCHEMA_VERSION:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, now_ts()),
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes ---------------------------------------------------------------

    def record_sample(self, ts: int, source: str, payload: Any) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO sample (ts, source, payload) VALUES (?, ?, ?)",
                (ts, source, json.dumps(payload, separators=(",", ":"), default=str)),
            )

    def record_quota(self, ts: int, readings: Iterable[QuotaReading]) -> int:
        rows = [(ts, r.window, r.label, r.pct, r.resets_at) for r in readings]
        with self._tx() as cur:
            cur.executemany(
                "INSERT OR REPLACE INTO quota (ts, window, label, pct, resets_at) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def record_overage(self, ts: int, reading: OverageReading) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO overage (ts, spent_minor, cap_minor, currency) "
                "VALUES (?, ?, ?, ?)",
                (ts, reading.spent_minor, reading.cap_minor, reading.currency),
            )

    def record_event(self, kind: str, detail: str, ts: int | None = None) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO event (ts, kind, detail) VALUES (?, ?, ?)",
                (ts if ts is not None else now_ts(), kind, detail),
            )

    # -- reads ----------------------------------------------------------------

    def latest_quota(self) -> list[QuotaRow]:
        """Most recent reading for every window ever seen."""
        with self._tx() as cur:
            rows = cur.execute(
                "SELECT q.ts, q.window, q.label, q.pct, q.resets_at FROM quota q "
                "JOIN (SELECT window, MAX(ts) AS ts FROM quota GROUP BY window) m "
                "ON q.window = m.window AND q.ts = m.ts ORDER BY q.window"
            ).fetchall()
        return [QuotaRow(**dict(r)) for r in rows]

    def quota_series(
        self, since_ts: int, window: str | None = None, until_ts: int | None = None
    ) -> list[QuotaRow]:
        sql = "SELECT ts, window, label, pct, resets_at FROM quota WHERE ts >= ?"
        params: list[Any] = [since_ts]
        if until_ts is not None:
            sql += " AND ts <= ?"
            params.append(until_ts)
        if window is not None:
            sql += " AND window = ?"
            params.append(window)
        sql += " ORDER BY ts, window"
        with self._tx() as cur:
            rows = cur.execute(sql, params).fetchall()
        return [QuotaRow(**dict(r)) for r in rows]

    def windows(self) -> list[str]:
        with self._tx() as cur:
            rows = cur.execute("SELECT DISTINCT window FROM quota ORDER BY window").fetchall()
        return [r["window"] for r in rows]

    def latest_overage(self) -> dict[str, Any] | None:
        with self._tx() as cur:
            row = cur.execute(
                "SELECT ts, spent_minor, cap_minor, currency FROM overage ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def recent_events(self, limit: int = 20, kind: str | None = None) -> list[EventRow]:
        sql = "SELECT ts, kind, detail FROM event"
        params: list[Any] = []
        if kind is not None:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY ts DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._tx() as cur:
            rows = cur.execute(sql, params).fetchall()
        return [EventRow(**dict(r)) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._tx() as cur:
            out = {}
            for table in ("quota", "sample", "overage", "event"):
                out[table] = cur.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        return out
