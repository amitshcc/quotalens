"""Sample retention: bounded growth that still keeps the endpoint-drift record."""

from __future__ import annotations

import asyncio
import sqlite3

from conftest import make_client, make_handler
from quotalens.config import PRUNE_EVERY_S
from quotalens.poller import Poller
from quotalens.secrets import Redactor
from quotalens.store import Store, key_signature

USAGE_SHAPE = {"five_hour": {"utilization": 1}, "seven_day": {"utilization": 2}}
DRIFTED_SHAPE = {"five_hour": {"utilization": 1}, "cinder_cove": None}


def test_key_signature_is_the_top_level_shape() -> None:
    assert key_signature({"b": 1, "a": 2}) == "a,b"
    assert key_signature({"a": {"deep": "changes are not drift"}}) == "a"
    assert key_signature([1, 2]) == "<list>"
    assert key_signature(None) == "<NoneType>"


def test_prune_keeps_the_newest_and_the_first_of_every_shape(tmp_path) -> None:
    store = Store(tmp_path / "p.db")
    store.record_sample(1, "usage", DRIFTED_SHAPE)  # oldest, and the only one of its shape
    for ts in range(2, 60):
        store.record_sample(ts, "usage", USAGE_SHAPE)
    store.record_sample(60, "overage", {"used_credits": 1})

    dry = store.prune_samples(keep_last=10, dry_run=True)
    assert dry.deleted == 0 and store.counts()["sample"] == 60  # nothing removed
    assert dry.candidates == 48  # but it says what it would have removed
    assert dry.kept == 12  # and what would be left, not what is there now
    assert dry.candidates + dry.kept == 60
    assert dry.signatures == 3  # two usage shapes and one overage shape

    result = store.prune_samples(keep_last=10)
    assert result.deleted == 48 and result.kept == 12  # ten newest plus two first-of-shape
    kept = (
        sqlite3.connect(store.path).execute("SELECT ts, keysig FROM sample ORDER BY ts").fetchall()
    )
    assert kept[0] == (1, "cinder_cove,five_hour")  # the novel shape survives forever
    assert kept[1][0] == 2  # and the first sample of the ordinary shape
    assert [ts for ts, _ in kept[2:]] == list(range(51, 61))
    store.close()


def test_a_dry_run_reports_the_same_size_the_real_run_starts_from(tmp_path) -> None:
    """The write-ahead log made a dry run report roughly double, and no change."""
    store = Store(tmp_path / "wal.db")
    payload = {"five_hour": {"utilization": 1}, "blob": "x" * 3000}
    for ts in range(1500):
        store.record_sample(ts, "usage", payload)
    dry = store.prune_samples(keep_last=100, dry_run=True)
    real = store.prune_samples(keep_last=100)
    assert dry.bytes_before == real.bytes_before  # the same starting point
    assert dry.bytes_after == dry.bytes_before  # a dry run changes nothing
    assert real.bytes_after is not None and real.bytes_after < real.bytes_before / 2
    assert dry.candidates == real.deleted and dry.kept == real.kept
    store.close()


def test_prune_shrinks_the_file_rather_than_growing_the_wal(tmp_path) -> None:
    store = Store(tmp_path / "big.db")
    payload = {"five_hour": {"utilization": 1}, "blob": "x" * 3000}
    for ts in range(2000):
        store.record_sample(ts, "usage", payload)
    before = store.db_size_bytes()
    result = store.prune_samples(keep_last=100)
    assert result.deleted == 1899
    assert result.bytes_after is not None and result.bytes_before is not None
    assert result.bytes_after < result.bytes_before  # and the number the user sees agrees
    assert store.db_size_bytes() < before / 2
    store.close()


def test_prune_is_idempotent_and_a_small_table_is_left_alone(tmp_path) -> None:
    store = Store(tmp_path / "small.db")
    for ts in range(1, 20):
        store.record_sample(ts, "usage", USAGE_SHAPE)
    assert store.prune_samples(keep_last=100).deleted == 0
    assert store.prune_samples(keep_last=10).deleted == 8  # 10 newest + 1 first-of-shape
    assert store.prune_samples(keep_last=10).deleted == 0
    store.close()


def test_migration_backfills_the_signature_of_existing_rows(tmp_path) -> None:
    """A database written before the column arrives still gets its drift record."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at INTEGER NOT NULL);"
        "INSERT INTO schema_version VALUES (4, 0);"
        "CREATE TABLE sample (ts INTEGER NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL);"
        "INSERT INTO sample VALUES (1, 'usage', '{\"b\": 1, \"a\": 2}');"
        "INSERT INTO sample VALUES (2, 'usage', 'not json at all');"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    rows = sqlite3.connect(path).execute("SELECT ts, keysig FROM sample ORDER BY ts").fetchall()
    assert rows == [(1, "a,b"), (2, "<NoneType>")]
    store.close()


def test_poller_prunes_on_a_schedule_not_every_poll(settings, store, secrets) -> None:
    clock = {"t": 1_000_000.0}
    poller = Poller(
        settings,
        store,
        secrets,
        Redactor(),
        client_factory=lambda c: make_client(make_handler(), c),
        clock=lambda: clock["t"],
    )
    calls: list[int] = []
    original = store.prune_samples

    def counting(keep_last: int, dry_run: bool = False):
        calls.append(keep_last)
        return original(keep_last, dry_run)

    store.prune_samples = counting  # type: ignore[method-assign]
    asyncio.run(poller.poll_once())
    assert calls == [settings.sample_keep]  # once at the first poll
    clock["t"] += 120
    asyncio.run(poller.poll_once())
    assert len(calls) == 1  # not again a couple of minutes later
    clock["t"] += PRUNE_EVERY_S
    asyncio.run(poller.poll_once())
    assert len(calls) == 2


def test_a_failing_prune_never_costs_a_reading(settings, store, secrets) -> None:
    def boom(keep_last: int, dry_run: bool = False):
        raise sqlite3.OperationalError("database is locked")

    store.prune_samples = boom  # type: ignore[method-assign]
    poller = Poller(
        settings,
        store,
        secrets,
        Redactor(),
        client_factory=lambda c: make_client(make_handler(), c),
    )
    assert asyncio.run(poller.poll_once()) == settings.poll_interval_s
    assert poller.status.state == "ok" and store.counts()["quota"] == 3
    assert "prune_failed" in [e.kind for e in store.recent_events()]
