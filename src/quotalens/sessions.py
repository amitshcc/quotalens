"""Derive 5-hour session windows from stored samples.

The 5-hour window is not a clock schedule: it starts at the first message and
expires five hours later. The API's ``five_hour.resets_at`` *is* the expiry of
the running window, so a jump forward means a new session started, and the start
is that value minus five hours. Samples taken after the expiry that still carry
the old value belong to no window: the account was idle.

Derivation is pure and idempotent; :func:`rebuild` replaces the table from all
stored samples, so a re-run never duplicates rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import Any

from quotalens.burn import split_at_resets
from quotalens.store import QuotaRow, Store

SESSION_LENGTH_S = 5 * 3600
RATE_WINDOW = "five_hour"
RESET_TOLERANCE_S = 60  # resets_at jitters by a second or so between polls
GAP_THRESHOLD_S = 180  # a longer silence than this counts as not observed


@dataclass(frozen=True)
class Delta:
    start: float
    end: float
    reset: bool  # the limit reset inside the session window; start->end is not a consumption

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "reset": self.reset}


@dataclass(frozen=True)
class SessionWindow:
    started_at: int
    ends_at: int
    is_current: bool
    peak_pct: float
    final_pct: float
    samples: int
    first_ts: int
    last_ts: int
    deltas: dict[str, Delta] = field(default_factory=dict)
    covered_s: int = 0  # seconds of the window during which the collector was running

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "ends_at": self.ends_at,
            "is_current": self.is_current,
            "peak_pct": self.peak_pct,
            "final_pct": self.final_pct,
            "samples": self.samples,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "deltas": {k: d.as_dict() for k, d in self.deltas.items()},
            "covered_s": self.covered_s,
        }


def _epoch(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp())


def _group_rate_rows(rows: list[QuotaRow]) -> list[list[tuple[QuotaRow, int]]]:
    """Session samples grouped by expiry (within tolerance), taken before that expiry.

    Groups are keyed by expiry rather than by run, so samples that alternate between
    two expiries (two collectors writing to one database) merge instead of producing
    two windows with the same start.
    """
    groups: list[list[tuple[QuotaRow, int]]] = []
    refs: list[int] = []
    for row in sorted(rows, key=lambda r: r.ts):
        ends = _epoch(row.resets_at)
        if ends is None:
            continue  # no window running
        index = next(
            (i for i in range(len(refs) - 1, -1, -1) if abs(ends - refs[i]) <= RESET_TOLERANCE_S),
            None,
        )
        if index is None:
            groups.append([])
            refs.append(ends)
            index = len(refs) - 1
        ref = refs[index]
        if row.ts > ref + RESET_TOLERANCE_S:
            continue  # the window expired and nothing new started: idle, not a sample of it
        groups[index].append((row, ref))
    return [g for g in groups if g]


def observed_seconds(timestamps: list[int], start: int, end: int, threshold_s: int) -> int:
    """Seconds of [start, end] during which samples kept arriving (gaps under the threshold)."""
    ts = sorted(t for t in timestamps if start <= t <= end)
    if not ts:
        return 0
    covered = 0
    for a, b in pairwise(ts):
        if b - a <= threshold_s:
            covered += b - a
    covered += min(threshold_s, ts[0] - start) + min(threshold_s, end - ts[-1])
    return min(covered, end - start)


def _delta(rows: list[QuotaRow], first_ts: int, last_ts: int) -> Delta | None:
    inside = [r for r in rows if first_ts <= r.ts <= last_ts]
    if not inside:
        return None
    return Delta(inside[0].pct, inside[-1].pct, len(split_at_resets(inside)) > 1)


def derive_sessions(rows_by_window: dict[str, list[QuotaRow]], now: int) -> list[SessionWindow]:
    """All session windows visible in the samples, oldest first."""
    groups = _group_rate_rows(rows_by_window.get(RATE_WINDOW, []))
    others = {
        w: sorted(rows, key=lambda r: r.ts)
        for w, rows in rows_by_window.items()
        if w != RATE_WINDOW
    }
    windows: list[SessionWindow] = []
    for index, group in enumerate(groups):
        ends_at = group[0][1]
        rows = sorted((r for r, _ in group), key=lambda r: r.ts)
        first_ts, last_ts = rows[0].ts, rows[-1].ts
        started_at = ends_at - SESSION_LENGTH_S
        covered = observed_seconds(
            [r.ts for r in rows], started_at, min(ends_at, now), GAP_THRESHOLD_S
        )
        deltas = {}
        for w, wrows in others.items():
            d = _delta(wrows, first_ts, last_ts)
            if d is not None:
                deltas[w] = d
        windows.append(
            SessionWindow(
                started_at=started_at,
                ends_at=ends_at,
                is_current=index == len(groups) - 1 and now < ends_at,
                peak_pct=max(r.pct for r in rows),
                final_pct=rows[-1].pct,
                samples=len(rows),
                first_ts=first_ts,
                last_ts=last_ts,
                deltas=deltas,
                covered_s=covered,
            )
        )
    return windows


def coverage_pct(covered_s: int, elapsed_s: int) -> float:
    """Share of the window's elapsed time that was observed, capped at 100."""
    if elapsed_s <= 0:
        return 100.0
    return min(100.0, round(covered_s / elapsed_s * 100, 1))


def idle_spans(windows: list[SessionWindow], now: int) -> list[tuple[int, int]]:
    """Spans where no session window was running, between and after known windows."""
    spans: list[tuple[int, int]] = []
    ordered = sorted(windows, key=lambda w: w.started_at)
    for prev, nxt in pairwise(ordered):
        if nxt.started_at > prev.ends_at:
            spans.append((prev.ends_at, nxt.started_at))
    if ordered and not ordered[-1].is_current and now > ordered[-1].ends_at:
        spans.append((ordered[-1].ends_at, now))
    return spans


def rebuild(store: Store, now: int) -> int:
    """Recompute the whole table from every stored sample. Returns the row count."""
    rows_by_window: dict[str, list[QuotaRow]] = {}
    for row in store.quota_series(0):
        rows_by_window.setdefault(row.window, []).append(row)
    windows = derive_sessions(rows_by_window, now)
    store.replace_sessions(windows)
    return len(windows)


def window_from_row(row: dict[str, Any]) -> SessionWindow:
    raw = json.loads(row["deltas"] or "{}")
    return SessionWindow(
        started_at=row["started_at"],
        ends_at=row["ends_at"],
        is_current=bool(row["is_current"]),
        peak_pct=row["peak_pct"],
        final_pct=row["final_pct"],
        samples=row["samples"],
        first_ts=row["first_ts"],
        last_ts=row["last_ts"],
        deltas={k: Delta(v["start"], v["end"], bool(v["reset"])) for k, v in raw.items()},
        covered_s=int(row.get("covered_s") or 0),
    )
