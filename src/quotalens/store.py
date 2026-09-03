"""SQLite storage. One file, stdlib ``sqlite3``, no ORM.

Schema follows PLAN.md. Migrations are ``CREATE TABLE IF NOT EXISTS`` plus a
``schema_version`` row; bump :data:`SCHEMA_VERSION` when a table changes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quotalens.parse import QuotaReading, SpendReading

SCHEMA_VERSION = 5

# Statements that bring an older database up to each version, in order.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE quota ADD COLUMN severity TEXT",
        "ALTER TABLE quota ADD COLUMN is_active INTEGER",
        "ALTER TABLE overage ADD COLUMN exponent INTEGER NOT NULL DEFAULT 2",
    ),
    3: (),  # session_window is created by the CREATE statements; rebuilt from samples
    4: ("ALTER TABLE session_window ADD COLUMN covered_s INTEGER NOT NULL DEFAULT 0",),
    5: ("ALTER TABLE sample ADD COLUMN keysig TEXT",),  # backfilled below
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at INTEGER NOT NULL
);
-- raw payloads, for debugging endpoint drift
CREATE TABLE IF NOT EXISTS sample (
    ts INTEGER NOT NULL,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    keysig TEXT
);
CREATE INDEX IF NOT EXISTS sample_ts ON sample (ts);
-- one row per window per poll
CREATE TABLE IF NOT EXISTS quota (
    ts INTEGER NOT NULL,
    window TEXT NOT NULL,
    label TEXT NOT NULL,
    pct REAL NOT NULL,
    resets_at TEXT,
    severity TEXT,
    is_active INTEGER,
    PRIMARY KEY (ts, window)
);
CREATE INDEX IF NOT EXISTS quota_window_ts ON quota (window, ts);
CREATE TABLE IF NOT EXISTS overage (
    ts INTEGER PRIMARY KEY,
    spent_minor INTEGER NOT NULL,
    cap_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    exponent INTEGER NOT NULL DEFAULT 2
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
-- 5-hour session windows derived from five_hour.resets_at; rebuilt idempotently
CREATE TABLE IF NOT EXISTS session_window (
    started_at INTEGER PRIMARY KEY,
    ends_at INTEGER NOT NULL,
    is_current INTEGER NOT NULL,
    peak_pct REAL NOT NULL,
    final_pct REAL NOT NULL,
    samples INTEGER NOT NULL,
    first_ts INTEGER NOT NULL,
    last_ts INTEGER NOT NULL,
    deltas TEXT NOT NULL,
    covered_s INTEGER NOT NULL DEFAULT 0
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
    severity: str | None = None
    is_active: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "window": self.window,
            "label": self.label,
            "pct": self.pct,
            "resets_at": self.resets_at,
            "severity": self.severity,
            "is_active": self.is_active,
        }


def _row_to_quota(row: sqlite3.Row) -> QuotaRow:
    data = dict(row)
    active = data.get("is_active")
    data["is_active"] = None if active is None else bool(active)
    return QuotaRow(**data)


@dataclass(frozen=True)
class EventRow:
    ts: int
    kind: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "kind": self.kind, "detail": self.detail}


_SESSION_UPSERT = (
    "INSERT OR REPLACE INTO session_window (started_at, ends_at, is_current, "
    "peak_pct, final_pct, samples, first_ts, last_ts, deltas, covered_s) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _session_row(w: Any) -> tuple[Any, ...]:
    return (
        w.started_at,
        w.ends_at,
        int(w.is_current),
        w.peak_pct,
        w.final_pct,
        w.samples,
        w.first_ts,
        w.last_ts,
        json.dumps({k: d.as_dict() for k, d in w.deltas.items()}),
        w.covered_s,
    )


def _backfill_keysig(cur: sqlite3.Cursor) -> None:
    """Give existing rows a signature, once, when the column arrives."""
    rows = cur.execute("SELECT rowid, payload FROM sample WHERE keysig IS NULL").fetchall()
    updates = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            payload = None
        updates.append((key_signature(payload), row["rowid"]))
    cur.executemany("UPDATE sample SET keysig = ? WHERE rowid = ?", updates)


def key_signature(payload: Any) -> str:
    """The payload's top-level shape: what changes when the endpoint drifts.

    Retention keeps the first sample of every signature forever, so this is the
    drift record and it has to survive pruning.
    """
    if isinstance(payload, dict):
        return ",".join(sorted(str(k) for k in payload))
    return f"<{type(payload).__name__}>"


@dataclass(frozen=True)
class PruneResult:
    candidates: int  # rows the retention rule selects; what a dry run would remove
    deleted: int  # rows actually removed: zero on a dry run
    kept: int
    signatures: int
    bytes_before: int | None = None
    bytes_after: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "deleted": self.deleted,
            "kept": self.kept,
            "signatures": self.signatures,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
        }


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
            had_tables = bool(
                cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
                ).fetchone()
            )
            cur.executescript(_SCHEMA)
            row = cur.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] if row and row["v"] is not None else 0
            if not had_tables:  # fresh file: the CREATE statements are already current
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, now_ts()),
                )
                return
            if current == 0:
                current = 1  # tables without a version row predate versioning
            for version in range(current + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS.get(version, ()):
                    try:
                        cur.execute(statement)
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc):
                            raise  # the CREATE statements already carry the column
                if version == 5:
                    _backfill_keysig(cur)
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, now_ts()),
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes ---------------------------------------------------------------

    def record_sample(self, ts: int, source: str, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str)
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO sample (ts, source, payload, keysig) VALUES (?, ?, ?, ?)",
                (ts, source, body, key_signature(payload)),
            )

    def record_quota(self, ts: int, readings: Iterable[QuotaReading]) -> int:
        rows = [
            (
                ts,
                r.window,
                r.label,
                r.pct,
                r.resets_at,
                r.severity,
                None if r.is_active is None else int(r.is_active),
            )
            for r in readings
        ]
        with self._tx() as cur:
            cur.executemany(
                "INSERT OR REPLACE INTO quota "
                "(ts, window, label, pct, resets_at, severity, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def record_overage(self, ts: int, spend: SpendReading) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO overage "
                "(ts, spent_minor, cap_minor, currency, exponent) VALUES (?, ?, ?, ?, ?)",
                (ts, spend.used_minor, spend.limit_minor or 0, spend.currency, spend.exponent),
            )

    def record_event(self, kind: str, detail: str, ts: int | None = None) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO event (ts, kind, detail) VALUES (?, ?, ?)",
                (ts if ts is not None else now_ts(), kind, detail),
            )

    def replace_sessions(self, windows: Iterable[Any]) -> None:
        """Replace the whole session_window table in one transaction (idempotent)."""
        rows = [_session_row(w) for w in windows]
        with self._tx() as cur:
            cur.execute("DELETE FROM session_window")
            cur.executemany(_SESSION_UPSERT, rows)

    def upsert_sessions(self, windows: Iterable[Any], demote_before: int | None = None) -> int:
        """Replace only the given windows, leaving every other row untouched.

        ``demote_before`` clears ``is_current`` on older rows, so the flag cannot be
        left behind on a window the incremental pass did not look at.
        """
        rows = [_session_row(w) for w in windows]
        with self._tx() as cur:
            if demote_before is not None:
                cur.execute(
                    "UPDATE session_window SET is_current = 0 "
                    "WHERE started_at < ? AND is_current = 1",
                    (demote_before,),
                )
            cur.executemany(_SESSION_UPSERT, rows)
        return len(rows)

    def sessions(self, limit: int = 50, order: str = "recent") -> list[dict[str, Any]]:
        order_sql = "peak_pct DESC, started_at DESC" if order == "consumed" else "started_at DESC"
        with self._tx() as cur:
            rows = cur.execute(
                f"SELECT * FROM session_window ORDER BY {order_sql} LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- reads ----------------------------------------------------------------

    def latest_quota(self) -> list[QuotaRow]:
        """Most recent reading for every window ever seen."""
        with self._tx() as cur:
            rows = cur.execute(
                "SELECT q.ts, q.window, q.label, q.pct, q.resets_at, q.severity, q.is_active "
                "FROM quota q "
                "JOIN (SELECT window, MAX(ts) AS ts FROM quota GROUP BY window) m "
                "ON q.window = m.window AND q.ts = m.ts ORDER BY q.window"
            ).fetchall()
        return [_row_to_quota(r) for r in rows]

    def quota_series(
        self, since_ts: int, window: str | None = None, until_ts: int | None = None
    ) -> list[QuotaRow]:
        sql = (
            "SELECT ts, window, label, pct, resets_at, severity, is_active FROM quota WHERE ts >= ?"
        )
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
        return [_row_to_quota(r) for r in rows]

    def windows(self) -> list[str]:
        with self._tx() as cur:
            rows = cur.execute("SELECT DISTINCT window FROM quota ORDER BY window").fetchall()
        return [r["window"] for r in rows]

    def latest_overage(self) -> dict[str, Any] | None:
        with self._tx() as cur:
            row = cur.execute(
                "SELECT ts, spent_minor, cap_minor, currency, exponent FROM overage "
                "ORDER BY ts DESC LIMIT 1"
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

    def oldest_ts(self) -> int | None:
        with self._tx() as cur:
            row = cur.execute("SELECT MIN(ts) AS t FROM quota").fetchone()
        return row["t"] if row else None

    def db_size_bytes(self) -> int | None:
        if not isinstance(self.path, Path):
            return None
        total = 0
        for suffix in ("", "-wal"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total

    def prune_samples(self, keep_last: int, dry_run: bool = False) -> PruneResult:
        """Keep the newest ``keep_last`` samples plus the first of every key signature.

        Unbounded ``sample`` growth is the bug in "leave it running": at 2-4 KB a
        payload and a minute a poll it is gigabytes a year. The signature rule is
        what lets the table be bounded without losing the endpoint-drift record.
        """
        keep_last = max(0, keep_last)
        # Fold the write-ahead log in first, or "before" includes megabytes of WAL
        # this very session wrote (a migration, say) and the report reads as a lie.
        self.checkpoint()
        before = self.db_size_bytes()
        with self._tx() as cur:
            signatures = cur.execute(
                "SELECT COUNT(DISTINCT source || '\x1f' || COALESCE(keysig, '')) FROM sample"
            ).fetchone()[0]
            doomed = cur.execute(
                "SELECT COUNT(*) FROM sample WHERE rowid NOT IN "
                "(SELECT rowid FROM sample ORDER BY ts DESC, rowid DESC LIMIT ?) "
                "AND rowid NOT IN "
                "(SELECT MIN(rowid) FROM sample GROUP BY source, COALESCE(keysig, ''))",
                (keep_last,),
            ).fetchone()[0]
            if not dry_run and doomed:
                cur.execute(
                    "DELETE FROM sample WHERE rowid NOT IN "
                    "(SELECT rowid FROM sample ORDER BY ts DESC, rowid DESC LIMIT ?) "
                    "AND rowid NOT IN "
                    "(SELECT MIN(rowid) FROM sample GROUP BY source, COALESCE(keysig, ''))",
                    (keep_last,),
                )
            remaining = cur.execute("SELECT COUNT(*) FROM sample").fetchone()[0]
        # On a dry run nothing was deleted, so say what *would* be left, not what is.
        kept = remaining - doomed if dry_run else remaining
        if not dry_run and doomed:
            self.vacuum()
        return PruneResult(
            candidates=doomed,
            deleted=0 if dry_run else doomed,
            kept=kept,
            signatures=signatures,
            bytes_before=before,
            bytes_after=self.db_size_bytes(),
        )

    def checkpoint(self) -> None:
        """Fold the write-ahead log back into the database file."""
        with self._lock:
            self._conn.isolation_level = None
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self._conn.isolation_level = ""

    def vacuum(self) -> None:
        """Reclaim the freed pages, then fold the WAL back into the file.

        Without the checkpoint the reclaimed space sits in the write-ahead log and
        the reported file size goes *up* after a prune, which reads as a bug.
        """
        with self._lock:
            self._conn.isolation_level = None
            try:
                self._conn.execute("VACUUM")
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self._conn.isolation_level = ""

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """One read, lock held only for its duration. Used by the paged exporter."""
        with self._tx() as cur:
            return cur.execute(sql, tuple(params)).fetchall()

    def counts(self) -> dict[str, int]:
        with self._tx() as cur:
            out = {}
            for table in ("quota", "sample", "overage", "event", "session_window"):
                out[table] = cur.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        return out
