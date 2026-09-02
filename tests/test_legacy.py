from __future__ import annotations

import sqlite3

from quotalens.legacy import migrate_legacy
from quotalens.secrets import MemorySecretStore
from quotalens.store import Store


def _make_db(path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(path)
    s.record_event("marker", marker, ts=1)
    s.close()


def _marker(path) -> str:
    conn = sqlite3.connect(path)
    value = conn.execute("SELECT detail FROM event WHERE kind='marker'").fetchone()[0]
    conn.close()
    return value


def test_old_database_is_moved_with_side_files(tmp_path) -> None:
    old = tmp_path / "quotawatch" / "quotawatch.db"
    new = tmp_path / "quotalens" / "quotalens.db"
    _make_db(old, "history")
    (tmp_path / "quotawatch" / "quotawatch.db-wal").write_bytes(b"")
    report = migrate_legacy(new, MemorySecretStore(None), MemorySecretStore(None), old_db=old)
    assert new.exists() and not old.exists()
    assert not (tmp_path / "quotawatch" / "quotawatch.db-wal").exists()
    assert _marker(new) == "history"
    assert any("moved readings database" in n for n in report.notes)


def test_old_and_new_both_present_keeps_both_and_uses_new(tmp_path) -> None:
    old = tmp_path / "old.db"
    new = tmp_path / "new.db"
    _make_db(old, "old-history")
    _make_db(new, "new-history")
    report = migrate_legacy(new, MemorySecretStore(None), MemorySecretStore(None), old_db=old)
    assert old.exists() and new.exists()
    assert _marker(new) == "new-history" and _marker(old) == "old-history"
    assert any("both" in n for n in report.notes)


def test_no_old_database_is_a_no_op(tmp_path) -> None:
    new = tmp_path / "new.db"
    report = migrate_legacy(
        new, MemorySecretStore(None), MemorySecretStore(None), old_db=tmp_path / "none.db"
    )
    assert not new.exists()
    assert report.notes == []


def test_keyring_entry_is_moved(tmp_path) -> None:
    old, new = MemorySecretStore("sessionKey=OLDOLDOLDOLD"), MemorySecretStore(None)
    migrate_legacy(tmp_path / "x.db", new, old, old_db=tmp_path / "none.db")
    assert new.get_cookie() == "sessionKey=OLDOLDOLDOLD"
    assert old.get_cookie() is None


def test_keyring_both_present_keeps_new(tmp_path) -> None:
    old, new = (
        MemorySecretStore("sessionKey=OLDOLDOLDOLD"),
        MemorySecretStore("sessionKey=NEWNEWNEWNEW"),
    )
    report = migrate_legacy(tmp_path / "x.db", new, old, old_db=tmp_path / "none.db")
    assert new.get_cookie() == "sessionKey=NEWNEWNEWNEW"
    assert old.get_cookie() == "sessionKey=OLDOLDOLDOLD"
    assert any("both" in n for n in report.notes)
