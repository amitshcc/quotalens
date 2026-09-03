"""CSV and JSON export. "Your data leaves easily" is a stated principle.

Every export streams: a month of history is a lot of rows and none of them need
to be in memory at once. The store is paged by rowid rather than held open under
a cursor, so a slow client cannot block the poller behind the write lock.

Derived tables are safe to hand to anyone. Raw ``sample`` rows are the response
bodies claude.ai sent, so they can carry organisation identifiers; they need an
explicit flag and they carry the same warning ``probe`` prints. The session
cookie appears in none of them: it travels in request headers, and nothing here
records a request.
"""

from __future__ import annotations

import csv
import io
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass

from quotalens.store import Store

RAW_WARNING = (
    "Raw samples are claude.ai's own responses. They contain no cookie, but they "
    "do carry organisation identifiers and reset timestamps: redact before sharing."
)
PAGE = 500


@dataclass(frozen=True)
class ExportSpec:
    table: str
    columns: tuple[str, ...]
    ts_column: str | None = "ts"
    raw: bool = False  # needs an explicit flag and carries the warning


EXPORTS: dict[str, ExportSpec] = {
    "quota": ExportSpec(
        "quota", ("ts", "window", "label", "pct", "resets_at", "severity", "is_active")
    ),
    "events": ExportSpec("event", ("ts", "kind", "detail")),
    "overage": ExportSpec("overage", ("ts", "spent_minor", "cap_minor", "currency", "exponent")),
    "sessions": ExportSpec(
        "session_window",
        (
            "started_at",
            "ends_at",
            "is_current",
            "peak_pct",
            "final_pct",
            "samples",
            "first_ts",
            "last_ts",
            "covered_s",
            "deltas",
        ),
        ts_column="started_at",
    ),
    "samples": ExportSpec("sample", ("ts", "source", "keysig", "payload"), raw=True),
}


class ExportError(ValueError):
    """An unknown table, or raw rows without the flag."""


def resolve(table: str, raw_allowed: bool) -> ExportSpec:
    spec = EXPORTS.get(table)
    if spec is None:
        raise ExportError(f"unknown table {table!r}; choose one of {', '.join(sorted(EXPORTS))}")
    if spec.raw and not raw_allowed:
        raise ExportError(
            f"{table} holds raw claude.ai responses; pass raw=1 to export them. {RAW_WARNING}"
        )
    return spec


def rows(store: Store, spec: ExportSpec, since_ts: int | None) -> Iterator[dict[str, object]]:
    """Every row, oldest first, a page at a time so nothing is buffered."""
    last_rowid = 0
    columns = ", ".join(spec.columns)
    where = f" AND {spec.ts_column} >= ?" if since_ts is not None and spec.ts_column else ""
    while True:
        params: list[object] = [last_rowid]
        if where:
            params.append(since_ts)
        # aliased: on a table whose INTEGER PRIMARY KEY *is* the rowid, selecting
        # "rowid" comes back under the other column's name.
        page = store.query(
            f"SELECT rowid AS _rid, {columns} FROM {spec.table} WHERE rowid > ?{where} "
            f"ORDER BY rowid LIMIT {PAGE}",
            params,
        )
        if not page:
            return
        for row in page:
            last_rowid = row["_rid"]
            yield {k: row[k] for k in spec.columns}


def csv_stream(store: Store, spec: ExportSpec, since_ts: int | None) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(spec.columns), lineterminator="\n")
    writer.writeheader()
    yield _drain(buffer)
    for row in rows(store, spec, since_ts):
        writer.writerow(row)
        yield _drain(buffer)


def _drain(buffer: io.StringIO) -> str:
    text = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    return text


def json_stream(store: Store, spec: ExportSpec, since_ts: int | None) -> Iterator[str]:
    head = {
        "table": spec.table,
        "generated_ts": int(time.time()),
        "since_ts": since_ts,
        "columns": list(spec.columns),
    }
    if spec.raw:
        head["warning"] = RAW_WARNING
    prefix = json.dumps(head)[:-1]  # drop the closing brace; the rows follow
    yield f'{prefix}, "rows": ['
    first = True
    for row in rows(store, spec, since_ts):
        yield ("" if first else ",") + json.dumps(row, default=str)
        first = False
    yield "]}"


def filename(spec: ExportSpec, suffix: str, profile: str = "") -> str:
    stamp = time.strftime("%Y%m%d", time.localtime())
    tag = f"-{profile}" if profile else ""
    return f"quotalens{tag}-{spec.table}-{stamp}.{suffix}"
