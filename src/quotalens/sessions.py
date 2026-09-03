"""Derive session windows from stored samples.

**This module rests on an inference, not on documentation.** Anthropic publishes
that the session limit "resets every five hours" and never publishes how the
window is anchored. We infer that it starts at the first message and expires five
hours later, so ``five_hour.resets_at`` is the expiry of the running window, a
jump forward means a new session started, and the start is that value minus five
hours. Samples taken after the expiry that still carry the old value belong to no
window: the account was idle.

The evidence for the inference is in this repository rather than in the docs: the
server recomputes ``resets_at`` on every call and only the sub-second part moves,
which is what a fixed anchor looks like and not what a sliding one would.

Because it is an inference, :func:`reset_model_violation` checks it on every poll
and says so loudly when it fails. See ``docs/FEATURE-REVIEW.md`` §2.3.

Derivation is pure and idempotent. :func:`rebuild` replaces the table from all
stored samples and runs at startup; :func:`rebuild_recent` runs on every poll and
touches only the current and previous windows, because re-deriving all of history
once a minute gets slowest for exactly the user this product is for.
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
INCREMENTAL_MARGIN_S = 600  # read a little before the previous window, for clock skew
MODEL_VIOLATION_KIND = "reset_model_violation"


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
    # The running window is the one with the newest sample, not the last group to appear.
    newest = max(range(len(groups)), key=lambda i: max(r.ts for r, _ in groups[i]), default=-1)
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
                is_current=index == newest and now < ends_at,
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


def reset_model_violation(
    prev_reset: str | None, prev_pct: float | None, cur_reset: str | None, cur_pct: float | None
) -> str | None:
    """Falsify the fixed-window inference, or return None.

    Under a fixed window a new expiry is at least five hours after the old one: a
    window can only begin at or after the previous one ended. So an expiry that
    moves *forward by less than five hours* while the percentage does not drop is
    a window extended in place, which the model says cannot happen. Anthropic's
    own tracker has reports of exactly that shape.

    A forward move under five hours *with* a large drop is also inconsistent, but
    it is far more likely a genuine new window plus a server-side correction, so
    it is left alone rather than reported as a broken model.
    """
    if not prev_reset or not cur_reset or prev_pct is None or cur_pct is None:
        return None
    before, after = _epoch(prev_reset), _epoch(cur_reset)
    if before is None or after is None:
        return None
    moved = after - before
    if not (RESET_TOLERANCE_S < moved < SESSION_LENGTH_S - RESET_TOLERANCE_S):
        return None
    if cur_pct < prev_pct:
        return None  # the percentage dropped: a new window, however oddly timed
    hours, minutes = divmod(moved // 60, 60)
    return (
        f"The session reset time moved forward {hours}h {minutes:02d}m without the "
        f"percentage dropping ({fmt_pct(prev_pct)}% then {fmt_pct(cur_pct)}%). QuotaLens "
        "infers a session's start from its reset time minus five hours; that inference "
        "does not hold here, so session history may be wrong."
    )


def fmt_pct(value: float) -> str:
    return f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"


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


def _rows_by_window(store: Store, since: int) -> dict[str, list[QuotaRow]]:
    rows_by_window: dict[str, list[QuotaRow]] = {}
    for row in store.quota_series(since):
        rows_by_window.setdefault(row.window, []).append(row)
    return rows_by_window


def window_sample_ts(store: Store, ends_at: int) -> list[int]:
    """Timestamps of the samples that produced the window expiring at ``ends_at``.

    A contaminated window cannot be removed by deleting a time range. Two
    collectors writing to one database interleave their samples second by second,
    which is exactly how contamination arises, so a range takes real rows with it.
    The expiry is what separates one collector's window from another's, and it is
    the same key :func:`_group_rate_rows` uses to build the window in the first
    place.
    """
    found: list[int] = []
    for row in store.quota_series(0):
        if row.window != RATE_WINDOW:
            continue
        ends = _epoch(row.resets_at)
        if ends is None or abs(ends - ends_at) > RESET_TOLERANCE_S:
            continue
        if row.ts > ends + RESET_TOLERANCE_S:
            continue  # taken after expiry: idle, and not part of this window
        found.append(row.ts)
    return sorted(found)


def rebuild(store: Store, now: int) -> int:
    """Recompute the whole table from every stored sample. Returns the row count.

    Startup and post-migration only: it reads every quota row.
    """
    windows = derive_sessions(_rows_by_window(store, 0), now)
    store.replace_sessions(windows)
    return len(windows)


def rebuild_recent(store: Store, now: int) -> int:
    """Update the current and previous windows from a tail of the samples.

    Equivalent to :func:`rebuild` over the same data, and tested to be, with one
    known limit: the tail cannot see an expiry that appears only before it. If
    ``resets_at`` ever moved *backwards* past the previous window, this pass would
    open a new group where the full pass extends an old one. The startup rebuild
    corrects that, and the reset-model watchdog is what would report it.
    """
    known = store.sessions(limit=2, order="recent")
    if len(known) < 2:
        return rebuild(store, now)  # nothing to be incremental about yet
    prev_start = int(known[1]["started_at"])
    tail = _rows_by_window(store, prev_start - INCREMENTAL_MARGIN_S)
    windows = [w for w in derive_sessions(tail, now) if w.started_at >= prev_start]
    return store.upsert_sessions(windows, demote_before=prev_start)


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
