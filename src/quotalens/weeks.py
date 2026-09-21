"""The Weeks ledger: one recorded event per weekly reset, with the cost as it stood.

The account's weekly limit is suspected of changing week to week. The payload never
exposes a ceiling (:mod:`quotalens.boost` documents this), so the only way to know is
to watch what a session costs the weekly limit and whether that ratio moves.
:mod:`quotalens.budget` computes the ratio, but as a single live median over all of
history — recomputed every request, never stored, never broken down by week. "8.5 full
sessions left" is then a number nobody can compare with last Monday's.

This module fixes that by writing one ``week_reset`` event when a weekly window's
``resets_at`` moves, whose detail is the just-closed week's cost figures **as they
stood at that reset** — computed over that week's session windows only (see
:func:`quotalens.budget.window_costs_by_week`), not over all history. A later reader
lines the weeks up and sees the shift and its uncertainty at once.

Modelled on :mod:`quotalens.boost`: a live detector on the poll path, a backfill that
walks stored ``quota`` rows and is idempotent on ``(closed_at, window)``, and the same
refusal to build a claim on an unverified (generic-fallback) reading.

What the ratio *cannot* say: whether a shift is the weekly pool getting larger or the
session pool being re-weighted. Both pools carry the same model prices, so mix cancels
and the two are indistinguishable here. The ledger shows the ratio with its spread and
says nothing stronger.
"""

from __future__ import annotations

import json
from collections.abc import Container, Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from statistics import median, quantiles
from typing import Any

from quotalens.boost import SHAPE_DRIFT_KIND
from quotalens.boost import recorded_ts as boost_recorded_ts
from quotalens.budget import week_key, window_costs_by_week
from quotalens.burn import resets_at_changed
from quotalens.parse import QuotaReading
from quotalens.runway import MIN_COMPARE_WINDOWS
from quotalens.sessions import SessionWindow, window_from_row
from quotalens.store import QuotaRow, now_ts

WEEK_RESET_KIND = "week_reset"
WEEK_LENGTH_S = 7 * 86400
RATE_WINDOW = "five_hour"  # the session window; its resets are the hero's business, not this
RECENT_LIMIT = 1000  # weekly resets are rare; this reaches back years


def is_weekly_window(window: str) -> bool:
    """Every window whose reset the ledger records: anything but the session window.

    ``unknown:`` windows are excluded too — they come from the generic tree walk, which
    the design already refuses to trust, and they carry no reliable reset time.
    """
    return window != RATE_WINDOW and not window.startswith("unknown:")


def _epoch(value: str | None) -> float | None:
    """Seconds since the epoch, keeping the sub-second part the reset slip lives in."""
    if not value:
        return None
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return None if dt.tzinfo is None else dt.timestamp()


def reset_slip_s(reset_at: str, next_reset_at: str) -> float | None:
    """How far the new reset is from exactly seven days after the old one.

    A changed schedule shows up here; the ordinary sub-second jitter reads as ~0.
    """
    before, after = _epoch(reset_at), _epoch(next_reset_at)
    if before is None or after is None:
        return None
    return round((after - before) - WEEK_LENGTH_S, 2)


@dataclass(frozen=True)
class WeekReset:
    """One weekly window's reset, and the cost of the week that just closed."""

    window: str
    label: str
    closed_at: int  # ts of the last reading of the old window
    closed_pct: float
    opened_pct: float
    reset_at: str  # the old resets_at
    next_reset_at: str  # the new resets_at
    reset_slip: float | None
    cost_per_full: float | None  # median weekly points for a session run to 100%, that week
    cost_low: float | None  # p25 of the same (already a cost-of-a-full-session figure)
    cost_high: float | None  # p75
    usable_windows: int  # complete session windows the week's estimate rests on
    full_windows_left: float | None  # sessions a fresh week's pool buys at this cost

    def detail(self) -> str:
        """The event text: a JSON object. Key order matches the documented shape."""
        return json.dumps(
            {
                "window": self.window,
                "label": self.label,
                "closed_at": self.closed_at,
                "closed_pct": self.closed_pct,
                "opened_pct": self.opened_pct,
                "reset_at": self.reset_at,
                "next_reset_at": self.next_reset_at,
                "reset_slip_s": self.reset_slip,
                "cost_per_full": self.cost_per_full,
                "cost_low": self.cost_low,
                "cost_high": self.cost_high,
                "usable_windows": self.usable_windows,
                "full_windows_left": self.full_windows_left,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_detail(cls, detail: str) -> WeekReset | None:
        """Rebuild from a stored event detail, or ``None`` if it is not one of ours."""
        try:
            data = json.loads(detail)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or "window" not in data or "closed_at" not in data:
            return None
        return cls(
            window=str(data["window"]),
            label=str(data.get("label") or data["window"]),
            closed_at=int(data["closed_at"]),
            closed_pct=float(data.get("closed_pct", 0.0)),
            opened_pct=float(data.get("opened_pct", 0.0)),
            reset_at=str(data.get("reset_at") or ""),
            next_reset_at=str(data.get("next_reset_at") or ""),
            reset_slip=_opt_float(data.get("reset_slip_s")),
            cost_per_full=_opt_float(data.get("cost_per_full")),
            cost_low=_opt_float(data.get("cost_low")),
            cost_high=_opt_float(data.get("cost_high")),
            usable_windows=int(data.get("usable_windows") or 0),
            full_windows_left=_opt_float(data.get("full_windows_left")),
        )


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


@dataclass(frozen=True)
class ResetTransition:
    """The bare fact of a weekly reset, before the cost of the closed week is measured."""

    window: str
    label: str
    closed_at: int
    closed_pct: float
    opened_pct: float
    reset_at: str
    next_reset_at: str


def detect_reset(
    previous: QuotaRow | None, current: QuotaReading, trusted: bool = True
) -> ResetTransition | None:
    """A weekly reset between two consecutive readings, or ``None`` and why not.

    Refused here so every consumer inherits one conclusion: an untrusted reading (the
    generic fallback recovered it), a non-weekly window, an undated block on either
    side, or a ``resets_at`` that did not actually move (only the jitter did).
    """
    if not trusted or previous is None or current.pct is None or previous.pct is None:
        return None
    if not is_weekly_window(current.window):
        return None
    if previous.resets_at is None or current.resets_at is None:
        return None
    if not resets_at_changed(previous.resets_at, current.resets_at):
        return None
    return ResetTransition(
        current.window,
        current.label or current.window,
        previous.ts,
        previous.pct,
        current.pct,
        previous.resets_at,
        current.resets_at,
    )


def detect_resets(
    previous: Iterable[QuotaRow], readings: Iterable[QuotaReading], trusted: bool = True
) -> list[ResetTransition]:
    """Every weekly window that reset between the last stored readings and these."""
    by_window = {row.window: row for row in previous}
    found = []
    for reading in readings:
        transition = detect_reset(by_window.get(reading.window), reading, trusted)
        if transition is not None:
            found.append(transition)
    return found


def _as_reading(row: QuotaRow) -> QuotaReading:
    return QuotaReading(row.window, row.label, row.pct, row.resets_at, row.severity, row.is_active)


def scan_history(
    rows_by_window: dict[str, list[QuotaRow]], untrusted_ts: Container[int]
) -> list[ResetTransition]:
    """Every weekly reset visible in stored readings, using the same rule as the live path.

    Consecutive rows for one window are the pair the live detector compares, so this
    walks them and calls the same function. Trust comes from the ``shape_drift`` events
    the poller writes at the instant a payload needed the generic fallback.
    """
    found: list[ResetTransition] = []
    for window, rows in rows_by_window.items():
        if not is_weekly_window(window):
            continue
        for previous, current in pairwise(sorted(rows, key=lambda r: r.ts)):
            transition = detect_reset(
                previous, _as_reading(current), current.ts not in untrusted_ts
            )
            if transition is not None:
                found.append(transition)
    return sorted(found, key=lambda t: (t.closed_at, t.window))


def _stats(per_full: list[float]) -> tuple[float | None, float | None, float | None]:
    """(median, p25, p75) of a week's per-full-session costs, or ``None`` below the floor.

    The same :data:`~quotalens.runway.MIN_COMPARE_WINDOWS` floor the live budget uses:
    a median drawn from fewer complete windows is the confident wrong number the whole
    budget derivation exists to refuse.
    """
    if len(per_full) < MIN_COMPARE_WINDOWS:
        return None, None, None
    q1, _q2, q3 = quantiles(per_full, n=4, method="inclusive")
    return round(float(median(per_full)), 2), round(q1, 2), round(q3, 2)


def week_reset(transition: ResetTransition, week_costs: Sequence[Any]) -> WeekReset:
    """Enrich a bare transition with the just-closed week's cost, as it stood then."""
    per_full = sorted(c.per_full_window for c in week_costs)
    cost, low, high = _stats(per_full)
    full_left = round(100.0 / cost, 2) if cost else None
    return WeekReset(
        window=transition.window,
        label=transition.label,
        closed_at=transition.closed_at,
        closed_pct=round(transition.closed_pct, 1),
        opened_pct=round(transition.opened_pct, 1),
        reset_at=transition.reset_at,
        next_reset_at=transition.next_reset_at,
        reset_slip=reset_slip_s(transition.reset_at, transition.next_reset_at),
        cost_per_full=cost,
        cost_low=low,
        cost_high=high,
        usable_windows=len(per_full),
        full_windows_left=full_left,
    )


def recorded(store: Any) -> list[Any]:
    """Every week-reset event the detector has written, newest first."""
    return store.recent_events(limit=RECENT_LIMIT, kind=WEEK_RESET_KIND)


def ledger(store: Any) -> list[WeekReset]:
    """The recorded resets as :class:`WeekReset`, most recent close first."""
    out = [WeekReset.from_detail(e.detail) for e in recorded(store)]
    return sorted((r for r in out if r is not None), key=lambda r: r.closed_at, reverse=True)


def _sessions(store: Any) -> list[SessionWindow]:
    return [window_from_row(r) for r in store.sessions(limit=1_000_000, order="recent")]


def _record(store: Any, transitions: list[ResetTransition], now: int) -> list[WeekReset]:
    """Enrich, deduplicate on ``(closed_at, window)``, and write. The one write path.

    Idempotent: a reset already recorded is skipped, nothing is updated or deleted. The
    event ts is the close moment, so the ledger pins each reset where the week ended.
    """
    already = {(r.closed_at, r.window) for r in ledger(store)}
    sessions = _sessions(store)
    boost_ts = boost_recorded_ts(store)
    by_key: dict[str, dict[str, list[Any]]] = {}
    written: list[WeekReset] = []
    for transition in transitions:
        if (transition.closed_at, transition.window) in already:
            continue
        if transition.window not in by_key:
            by_key[transition.window] = window_costs_by_week(
                sessions, transition.window, now, boost_ts
            )
        week_costs = by_key[transition.window].get(week_key(transition.closed_at), [])
        reset = week_reset(transition, week_costs)
        store.record_event(WEEK_RESET_KIND, reset.detail(), ts=transition.closed_at)
        already.add((transition.closed_at, transition.window))
        written.append(reset)
    return written


def backfill(store: Any, now: int | None = None) -> list[WeekReset]:
    """Record weekly resets that happened before the detector existed. Safe to run twice.

    Runs on start, the way :func:`quotalens.sessions.rebuild` does, so the ledger is
    present without anyone remembering a command. A repeat start costs a scan and writes
    nothing.
    """
    now = now_ts() if now is None else now
    untrusted = {int(e.ts) for e in store.recent_events(limit=10_000, kind=SHAPE_DRIFT_KIND)}
    rows_by_window: dict[str, list[QuotaRow]] = {}
    for row in store.quota_series(0):
        if is_weekly_window(row.window):
            rows_by_window.setdefault(row.window, []).append(row)
    return _record(store, scan_history(rows_by_window, untrusted), now)


def record_live(
    store: Any,
    previous: Iterable[QuotaRow],
    readings: Iterable[QuotaReading],
    now: int,
    trusted: bool,
) -> list[WeekReset]:
    """The poll-path detector: record any weekly reset between ``previous`` and these readings."""
    if not trusted:
        return []  # the payload needed the generic fallback; a claim must not rest on it
    return _record(store, detect_resets(previous, readings, trusted), now)
