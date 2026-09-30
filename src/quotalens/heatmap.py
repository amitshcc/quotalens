"""When you use it: Weekly — all models points gained per hour, by weekday and local hour.

Each cell is the average over the last four complete weeks of the points weekly-all
gained in that local hour, averaged over the weeks in which that hour was collected
(an hour the collector missed is not a zero). Below two complete weeks there is no
average worth drawing, and the section says it is collecting.

Bucketing, chosen from the stored data (WP-31): readings arrive every minute or so --
26,158 of 26,277 intervals over the three complete weeks were under two minutes, and
the gaps of ten minutes or more carried 10 of the 297 points gained. So a gain is
attributed to the hour it was observed in, and across the rare longer gap it is spread
over the hours the gap spans in proportion to time. A gap over six hours is not
attributed at all: its hours count as not collected. There is no smoothing across
neighbouring hours. Usage here is bursty -- five-hour sessions in working hours and
nothing overnight -- and smoothing would paint usage into hours that had none. The noise
it would remove is the whole-percent step, about one point per cell per week, which
averaging over the weeks already damps.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from quotalens.pace import complete_weeks, week_bounds
from quotalens.store import QuotaRow

WEEKS = 4
MIN_WEEKS = 2
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
HOUR_S = 3600
MAX_ATTRIBUTE_GAP_S = 6 * HOUR_S  # longer than this, the gain is not placed in any hour
OBSERVED_S = HOUR_S // 2  # an hour counts as collected with half of it covered
LEVELS = 4  # ramp steps above zero; the legend shows each upper edge
TOP_QUANTILE = 0.9  # the ramp tops out here, so one busy hour does not wash out the rest
EPOCH_WEEKDAY = 3  # 1970-01-01 was a Thursday; Monday is 0

_CACHE: dict[tuple[str, int], Heatmap] = {}
_CACHE_MAX = 8


@dataclass(frozen=True)
class Heatmap:
    collecting: bool
    weeks_used: int
    cells: list[list[float | None]]  # [weekday Mon=0][local hour]; None = never collected
    top: float  # the ramp's top edge, points/hour; cells above it take the darkest step
    max_pts: float
    tz: str

    def level(self, value: float | None) -> int | None:
        """0 for nothing gained, 1..LEVELS up the ramp, None for not collected."""
        if value is None:
            return None
        if value <= 0:
            return 0
        return min(LEVELS, max(1, math.ceil(value / self.top * LEVELS)))

    def edges(self) -> list[float]:
        return [self.top * k / LEVELS for k in range(1, LEVELS + 1)]

    def as_dict(self) -> dict[str, Any]:
        return {
            "collecting": self.collecting,
            "weeks_used": self.weeks_used,
            "min_weeks": MIN_WEEKS,
            "window": "seven_day",
            "unit": "points per hour",
            "timezone": self.tz,
            "days": list(DAYS),
            "rows": [
                {
                    "day": DAYS[d],
                    "hours": [None if v is None else round(v, 2) for v in self.cells[d]],
                }
                for d in range(7)
            ],
            "top": self.top,
            "max": round(self.max_pts, 2),
        }


def _cell(ts: int) -> tuple[int, int]:
    """(weekday, hour) in local time, Monday = 0."""
    local = ts + time.localtime(ts).tm_gmtoff
    return (local // 86400 + EPOCH_WEEKDAY) % 7, (local // HOUR_S) % 24


def _week_cells(rows: Sequence[QuotaRow]) -> tuple[list[list[float]], list[list[float]]]:
    """(points gained, seconds collected) per cell for one week of readings."""
    gained = [[0.0] * 24 for _ in DAYS]
    seen = [[0.0] * 24 for _ in DAYS]
    for a, b in pairwise(rows):
        span = b.ts - a.ts
        if span <= 0 or span > MAX_ATTRIBUTE_GAP_S:
            continue
        # A fall is a boost, not use: it gains nothing, and the time still counts as seen.
        delta = max(0.0, b.pct - a.pct)
        t = a.ts
        while t < b.ts:
            edge = min(b.ts, (t // HOUR_S + 1) * HOUR_S)
            d, h = _cell(t)
            seen[d][h] += edge - t
            gained[d][h] += delta * (edge - t) / span
            t = edge
    return gained, seen


def build(weeks: Sequence[Sequence[QuotaRow]]) -> Heatmap:
    """The heatmap from up to four complete weeks of weekly-all readings."""
    tz = time.strftime("%Z")
    if len(weeks) < MIN_WEEKS:
        empty: list[list[float | None]] = [[None] * 24 for _ in DAYS]
        return Heatmap(True, len(weeks), empty, 1.0, 0.0, tz)
    total = [[0.0] * 24 for _ in DAYS]
    count = [[0] * 24 for _ in DAYS]
    for rows in weeks:
        gained, seen = _week_cells(rows)
        for d in range(7):
            for h in range(24):
                if seen[d][h] >= OBSERVED_S:
                    total[d][h] += gained[d][h]
                    count[d][h] += 1
    cells: list[list[float | None]] = [
        [total[d][h] / count[d][h] if count[d][h] else None for h in range(24)] for d in range(7)
    ]
    busy = sorted(v for row in cells for v in row if v)
    max_pts = busy[-1] if busy else 0.0
    # The top of the ramp: the 90th percentile of the hours with any use, rounded up to
    # half a point, so the legend reads in round numbers and one outlier cannot flatten it.
    q = busy[min(len(busy) - 1, int(len(busy) * TOP_QUANTILE))] if busy else 0.0
    top = max(0.5, math.ceil(q * 2) / 2)
    return Heatmap(False, len(weeks), cells, top, max_pts, tz)


def compute_heatmap(store: Any, now: int) -> Heatmap:
    """The heatmap for the weeks before the current weekly-all window; cached per week."""
    bounds = week_bounds(store.latest_quota())
    if bounds is None:
        return build([])
    start = bounds[0] if bounds[1] > now else bounds[1]  # a reset already passed: that week ended
    key = (str(store.path), start)
    if key[0] != ":memory:" and key in _CACHE:
        return _CACHE[key]
    heat = build([w.rows for w in complete_weeks(store, start, WEEKS)])
    if key[0] != ":memory:":
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = heat
    return heat
