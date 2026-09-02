"""Burn rate: percentage points of quota per hour.

The subtle part is the window reset. When a 5-hour window rolls over the
percentage drops sharply; that is a discontinuity, not a negative rate. We cut
the series into segments at every reset and only measure inside the newest one.

A reset is detected between two consecutive samples when either:

* ``resets_at`` moved by more than :data:`RESET_TIME_TOLERANCE_S` (both values
  present), or
* the percentage fell by more than :data:`RESET_DROP_PCT` points.

The second rule covers payloads where ``resets_at`` is absent or null. The
tolerance exists because the live server recomputes ``resets_at`` on every call
and the microseconds jitter (``12:40:00.421772`` then ``12:40:00.656558``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from quotalens.store import QuotaRow

RESET_DROP_PCT = 5.0
RESET_TIME_TOLERANCE_S = 60.0  # resets_at jitter below this is noise, not a new window
MIN_SPAN_S = 60  # two samples closer than this give a meaningless rate


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def resets_at_changed(prev: str, cur: str) -> bool:
    """True if two ``resets_at`` values denote different windows, ignoring jitter."""
    if prev == cur:
        return False
    a, b = _parse_iso(prev), _parse_iso(cur)
    if a is None or b is None or (a.tzinfo is None) != (b.tzinfo is None):
        return prev.split(".")[0] != cur.split(".")[0]  # unparsable: drop fractional seconds
    return abs((b - a).total_seconds()) > RESET_TIME_TOLERANCE_S


@dataclass(frozen=True)
class BurnResult:
    window: str
    rate_pct_per_hour: float | None
    points: int
    span_s: int
    from_ts: int | None
    to_ts: int | None
    from_pct: float | None
    to_pct: float | None
    segment_start_ts: int | None
    reason: str | None  # why the rate is None, or None when it is valid

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "rate_pct_per_hour": self.rate_pct_per_hour,
            "points": self.points,
            "span_seconds": self.span_s,
            "from_ts": self.from_ts,
            "to_ts": self.to_ts,
            "from_pct": self.from_pct,
            "to_pct": self.to_pct,
            "segment_start_ts": self.segment_start_ts,
            "reason": self.reason,
        }


def is_reset(prev: QuotaRow, cur: QuotaRow) -> bool:
    """True if a window boundary lies between ``prev`` and ``cur``."""
    if prev.resets_at and cur.resets_at and resets_at_changed(prev.resets_at, cur.resets_at):
        return True
    return (prev.pct - cur.pct) > RESET_DROP_PCT


def split_at_resets(rows: Sequence[QuotaRow]) -> list[list[QuotaRow]]:
    """Split one window's chronologically-sorted rows into reset-free segments."""
    segments: list[list[QuotaRow]] = []
    current: list[QuotaRow] = []
    for row in rows:
        if current and is_reset(current[-1], row):
            segments.append(current)
            current = []
        current.append(row)
    if current:
        segments.append(current)
    return segments


def _empty(window: str, reason: str, segment_start: int | None = None) -> BurnResult:
    return BurnResult(window, None, 0, 0, None, None, None, None, segment_start, reason)


def burn_rate(window: str, rows: Sequence[QuotaRow], lookback_s: int, now: int) -> BurnResult:
    """Rate over the last ``lookback_s`` seconds, within the newest reset-free segment.

    ``rows`` must be a single window, sorted by ``ts`` ascending.
    """
    if not rows:
        return _empty(window, "no readings")
    segment = split_at_resets(rows)[-1]
    segment_start = segment[0].ts
    cutoff = now - lookback_s
    recent = [r for r in segment if r.ts >= cutoff]
    if len(recent) < 2:
        return _empty(
            window,
            "fewer than 2 readings in lookback since last reset",
            segment_start,
        )
    first, last = recent[0], recent[-1]
    span = last.ts - first.ts
    if span < MIN_SPAN_S:
        return _empty(window, f"readings span under {MIN_SPAN_S}s", segment_start)
    rate = (last.pct - first.pct) / (span / 3600.0)
    return BurnResult(
        window=window,
        rate_pct_per_hour=round(rate, 3),
        points=len(recent),
        span_s=span,
        from_ts=first.ts,
        to_ts=last.ts,
        from_pct=first.pct,
        to_pct=last.pct,
        segment_start_ts=segment_start,
        reason=None,
    )
