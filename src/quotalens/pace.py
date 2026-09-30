"""Where Weekly — all models lands at the reset, projected from the week so far. An estimate.

The model was chosen by back-testing on the stored weeks (WP-31): at 24, 48, 72 and
96 hours into every complete week, three candidates projected the close --

- *linear*: the week so far, scaled to 168 hours;
- *typical shape*: the week so far divided by the median fraction other weeks had
  reached by the same hour;
- *typical remaining use*: the week so far plus the median of what other weeks went on
  to use from the same hour to their reset.

All three had a median absolute error of 1 point, because this account closes most
weeks at 99-100% and the ceiling flattens every hit to the same score. On the mean the
last one won clearly (4.6 points against 14.3 linear and 16.2 shape), and it placed
the moment a week ran out within a median 4 hours against linear's 12. Early in a
week the difference is large: 2% after one day is 14% by the straight line and 89% by
what the other weeks did next, and that week closed at 100%.

So the central figure is *typical remaining use*. The spread is the lowest and highest
of the per-week estimates with the linear one added, which covered the actual close
in 9 of 12 back-tests; with three weeks of history it cannot claim more than that,
which is why it is always shown and never a single number.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import pairwise
from statistics import median, median_low
from typing import Any

from quotalens.store import QuotaRow

PACE_WINDOW = "seven_day"  # weekly, all models: the pool everything draws on
WEEK_S = 7 * 86400
HOUR_S = 3600
WEEK_HOURS = 168
MIN_ELAPSED_S = 24 * HOUR_S  # below a day in, the week so far says too little
MAX_PRIOR_WEEKS = 4
OUT_PCT = 100.0
# A week counts as complete when its readings reach from near its opening to near its
# reset. Three hours at the front: the first reading after a reset can lag a restart.
OPEN_SLACK_S = 3 * HOUR_S
CLOSE_SLACK_S = 2 * HOUR_S
# A mid-week fall this large is a boost (FINDINGS: weekly-all 98 -> 0 on 5 Sep); what
# such a week "went on to use" is not a shape any other week will repeat.
BOOST_DROP_PTS = 5.0

# Prior weeks have ended, so their curves never change: read them once per week rather
# than on every render (four weeks of per-minute rows doubled the dashboard's build time).
_PRIOR: dict[tuple[str, int], list[list[float]]] = {}
_PRIOR_MAX = 8

HIDDEN_EARLY = "Pace shows from 24 hours into the week."
HIDDEN_NO_HISTORY = "Pace needs one complete week of history."
HIDDEN_WITHHELD = "No pace while the weekly reading is not trusted."
HIDDEN_NO_READING = "No weekly reading yet."
HIDDEN_AT_LIMIT = "The weekly limit is already reached."


@dataclass(frozen=True)
class CompleteWeek:
    start: int
    end: int
    rows: list[QuotaRow]


@dataclass(frozen=True)
class Pace:
    shown: bool
    reason: str  # why it is hidden; "" when shown
    now_pct: float | None = None
    elapsed_h: float | None = None
    week_start: int | None = None
    reset_ts: int | None = None
    end_pct: float | None = None  # central estimate of weekly-all at the reset, capped at 100
    end_low: float | None = None
    end_high: float | None = None
    runs_out: bool = False
    runs_out_ts: int | None = None  # central estimate of when it reaches 100%
    runs_out_low_ts: int | None = None  # earliest estimate
    runs_out_high_ts: int | None = None  # latest; None when some estimate never runs out
    weeks_used: int = 0
    sentence: str = ""
    basis: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "shown": self.shown,
            "reason": self.reason,
            "estimate": True,
            "window": PACE_WINDOW,
            "now_pct": self.now_pct,
            "elapsed_h": None if self.elapsed_h is None else round(self.elapsed_h, 1),
            "week_start_ts": self.week_start,
            "reset_ts": self.reset_ts,
            "end_pct": _r(self.end_pct),
            "end_low": _r(self.end_low),
            "end_high": _r(self.end_high),
            "runs_out": self.runs_out,
            "runs_out_ts": self.runs_out_ts,
            "runs_out_low_ts": self.runs_out_low_ts,
            "runs_out_high_ts": self.runs_out_high_ts,
            "weeks_used": self.weeks_used,
            "method": "typical remaining use: now + median of prior weeks' use from this hour",
            "sentence": self.sentence,
            "basis": self.basis,
        }


def _r(value: float | None) -> float | None:
    return None if value is None else round(value)


# -- the weeks behind this one -----------------------------------------------------


def week_bounds(latest: Sequence[QuotaRow]) -> tuple[int, int] | None:
    """(start, reset) of the current weekly-all window, on the whole hour.

    The vendor's reset time jitters by a second or so per poll; the week is named by
    the hour it resets on, as the ledger does.
    """
    row = next((r for r in latest if r.window == PACE_WINDOW and r.resets_at), None)
    if row is None or row.resets_at is None:
        return None
    try:
        reset = datetime.fromisoformat(row.resets_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    end = round(reset / HOUR_S) * HOUR_S
    return end - WEEK_S, end


def complete_weeks(store: Any, week_start: int, limit: int = MAX_PRIOR_WEEKS) -> list[CompleteWeek]:
    """Up to ``limit`` weekly-all windows before ``week_start``, newest first, fully observed.

    Fully observed: a reading within three hours of the opening and within two of the
    reset. A week the collector joined halfway through is left out rather than guessed.
    """
    out: list[CompleteWeek] = []
    for k in range(1, limit + 1):
        start, end = week_start - k * WEEK_S, week_start - (k - 1) * WEEK_S
        rows = store.quota_series(start, window=PACE_WINDOW, until_ts=end - 1)
        if rows and rows[0].ts <= start + OPEN_SLACK_S and rows[-1].ts >= end - CLOSE_SLACK_S:
            out.append(CompleteWeek(start, end, rows))
    return out


def hourly(rows: Sequence[QuotaRow], start: int) -> list[float]:
    """The week as 169 values, hour 0 to hour 168: the last reading at or before each hour.

    Hours before the first reading take its value (the first reading after a reset is
    0-2%, FINDINGS), so every hour has a number.
    """
    out: list[float] = []
    i, last = 0, rows[0].pct
    for h in range(WEEK_HOURS + 1):
        edge = start + h * HOUR_S
        while i < len(rows) and rows[i].ts <= edge:
            last = rows[i].pct
            i += 1
        out.append(last)
    return out


def _boosted(curve: Sequence[float]) -> bool:
    return any(a - b >= BOOST_DROP_PTS for a, b in pairwise(curve))


# -- the projection ------------------------------------------------------------------


def project(
    now_pct: float,
    now: int,
    week_start: int,
    reset_ts: int,
    prior: Sequence[Sequence[float]],
) -> Pace:
    """Project the close from ``now_pct`` at ``now``, given prior weeks' hourly curves."""
    elapsed_s = now - week_start
    base = {"now_pct": now_pct, "week_start": week_start, "reset_ts": reset_ts}
    if elapsed_s < MIN_ELAPSED_S:
        return Pace(False, HIDDEN_EARLY, elapsed_h=elapsed_s / HOUR_S, **base)
    if not prior:
        return Pace(False, HIDDEN_NO_HISTORY, elapsed_h=elapsed_s / HOUR_S, **base)
    if now_pct >= OUT_PCT:
        return Pace(False, HIDDEN_AT_LIMIT, elapsed_h=elapsed_s / HOUR_S, **base)
    elapsed_h = min(elapsed_s / HOUR_S, float(WEEK_HOURS))
    h = int(elapsed_h)

    ends = [now_pct + (c[WEEK_HOURS] - c[h]) for c in prior]
    linear = now_pct * WEEK_HOURS / elapsed_h
    end_pct = min(median(ends), OUT_PCT)
    end_low = max(min(min(ends), linear, OUT_PCT), now_pct)
    end_high = min(max(max(ends), linear), OUT_PCT)

    # When each estimate reaches 100%: the prior week's climb from this hour, laid on
    # top of where this week is now; and the straight line.
    def at_hour(t: int) -> int:
        return min(week_start + t * HOUR_S, reset_ts)

    crossings: list[float] = []
    for c in prior:
        t = next(
            (t for t in range(h + 1, WEEK_HOURS + 1) if now_pct + c[t] - c[h] >= OUT_PCT), None
        )
        crossings.append(math.inf if t is None else at_hour(t))
    # On the same hour grid as the weekly curves, so every time reads on the hour.
    linear_h = math.ceil(elapsed_h * OUT_PCT / now_pct) if now_pct > 0 else math.inf
    linear_cross = at_hour(linear_h) if linear_h <= WEEK_HOURS else math.inf
    # At least half the weeks run out: the lower median, so an even split says so.
    central = median_low(sorted(crossings))
    every = [*crossings, linear_cross]
    finite = [x for x in every if x != math.inf]
    runs_out = central != math.inf
    pace = Pace(
        True,
        "",
        now_pct=now_pct,
        elapsed_h=elapsed_h,
        week_start=week_start,
        reset_ts=reset_ts,
        end_pct=end_pct,
        end_low=end_low,
        end_high=end_high,
        runs_out=runs_out,
        runs_out_ts=int(central) if runs_out else None,
        runs_out_low_ts=int(min(finite)) if runs_out and finite else None,
        runs_out_high_ts=int(max(finite)) if runs_out and len(finite) == len(every) else None,
        weeks_used=len(prior),
    )
    return _with_words(pace)


def _day_time(ts: int) -> str:
    """ "Thu 14:00", local time: the week never spans more than seven days."""
    return datetime.fromtimestamp(ts).astimezone().strftime("%a %H:%M")


def _with_words(p: Pace) -> Pace:
    n = p.weeks_used
    basis = (
        f"An estimate from how the last {n} complete week{'s' if n != 1 else ''} "
        "went on from this point in the week."
    )
    if p.runs_out and p.runs_out_ts is not None:
        low = _day_time(p.runs_out_low_ts or p.runs_out_ts)
        high = _day_time(p.runs_out_high_ts) if p.runs_out_high_ts else "not before the reset"
        sentence = (
            f"At this pace the week runs out around {_day_time(p.runs_out_ts)} ({low} – {high})."
        )
    else:
        sentence = (
            f"At this pace the week ends near {p.end_pct:.0f}% ({p.end_low:.0f}–{p.end_high:.0f}%)."
        )
    return replace(p, sentence=sentence, basis=basis)


def compute_pace(store: Any, now: int, withheld: bool = False) -> Pace:
    """The pace for the current week, read from the store."""
    if withheld:
        return Pace(False, HIDDEN_WITHHELD)
    latest = store.latest_quota()
    bounds = week_bounds(latest)
    row = next((r for r in latest if r.window == PACE_WINDOW), None)
    if bounds is None or row is None:
        return Pace(False, HIDDEN_NO_READING)
    start, reset = bounds
    return project(row.pct, min(now, reset), start, reset, prior_curves(store, start))


def prior_curves(store: Any, week_start: int) -> list[list[float]]:
    """Hourly curves of the complete, unboosted weeks before ``week_start``; cached."""
    key = (str(store.path), week_start)
    if key[0] == ":memory:":  # not one database across instances; nothing to key on
        return _read_prior(store, week_start)
    if key not in _PRIOR:
        if len(_PRIOR) >= _PRIOR_MAX:
            _PRIOR.clear()
        _PRIOR[key] = _read_prior(store, week_start)
    return _PRIOR[key]


def _read_prior(store: Any, week_start: int) -> list[list[float]]:
    return [
        curve
        for w in complete_weeks(store, week_start)
        if not _boosted(curve := hourly(w.rows, w.start))
    ]
