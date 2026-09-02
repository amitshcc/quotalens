"""First-start migration from the project's previous name, quotawatch.

Readings collected under the old name are the one thing that must not be lost.
The database file is moved (with its WAL/SHM side files) and the keyring entry
is copied then removed. If both old and new exist, nothing is touched and the
situation is reported so the user can decide.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from quotalens.config import default_data_dir
from quotalens.secrets import SecretStore, SecretStoreError

log = logging.getLogger(__name__)

LEGACY_APP_NAME = "quotawatch"
LEGACY_KEYRING_SERVICE = "quotawatch"
LEGACY_ENV_PREFIX = "QUOTAWATCH_"
_SIDE_FILES = ("", "-wal", "-shm", "-journal")


def legacy_db_path() -> Path:
    return default_data_dir(LEGACY_APP_NAME) / f"{LEGACY_APP_NAME}.db"


@dataclass
class MigrationReport:
    notes: list[str] = field(default_factory=list)

    def add(self, note: str) -> None:
        self.notes.append(note)
        log.info("migration: %s", note)


def migrate_database(old_path: Path, new_path: Path, report: MigrationReport) -> None:
    if not old_path.exists():
        return
    if new_path.exists():
        report.add(
            f"both {old_path} and {new_path} exist; using the new one and leaving the old "
            "in place. Delete the old file yourself once you are sure."
        )
        return
    new_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in _SIDE_FILES:
        src = Path(str(old_path) + suffix)
        if src.exists():
            shutil.move(str(src), str(new_path) + suffix)
    report.add(f"moved readings database {old_path} -> {new_path}")


def migrate_keyring(old: SecretStore, new: SecretStore, report: MigrationReport) -> None:
    try:
        old_cookie = old.get_cookie()
    except SecretStoreError as exc:
        report.add(f"could not read the old keyring entry: {exc}")
        return
    if not old_cookie:
        return
    try:
        if new.get_cookie():
            report.add(
                "a session cookie exists under both the old and the new keyring entry; "
                "keeping the new one and leaving the old untouched"
            )
            return
        new.set_cookie(old_cookie)
    except SecretStoreError as exc:
        report.add(f"could not move the keyring entry: {exc}")
        return
    try:
        old.delete_cookie()
    except SecretStoreError:
        report.add("copied the session cookie to the new keyring entry; the old one remains")
        return
    report.add("moved the session cookie to the new keyring entry")


def migrate_legacy(
    new_db: Path, new_secrets: SecretStore, old_secrets: SecretStore, old_db: Path | None = None
) -> MigrationReport:
    report = MigrationReport()
    migrate_database(old_db if old_db is not None else legacy_db_path(), new_db, report)
    migrate_keyring(old_secrets, new_secrets, report)
    return report
