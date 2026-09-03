"""Runway: will the 5-hour window run out before it resets?

Pure functions over the current reading, the burn rate, and the window's
expiry. Everything here is what the user would otherwise compute in their head.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median

from quotalens.burn import split_at_resets
from quotalens.store import QuotaRow

SESSION_LENGTH_S = 5 * 3600
HOURS_PER_WINDOW = 5
FLAT_RATE_PTS_PER_HOUR = 0.05
MIN_COMPARE_WINDOWS = 5  # fewer complete windows than this: say nothing about the median
MIN_BUCKET_SAMPLES = 2


def clock(ts: int) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")


def fmt_span(seconds: float) -> str:
    """'2h 14m', '14m', '0m'."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def sustainable_rate(headroom_pct: float | None, remaining_s: int) -> float | None:
    """Points per hour that would exactly exhaust the window at its reset."""
    if headroom_pct is None or remaining_s <= 0:
        return None
    return round(headroom_pct / (remaining_s / 3600), 2)


def project(
    pct: float, rate: float | None, now: int, reset_ts: int | None
) -> tuple[int | None, float | None]:
    """(exhaust_ts, finish_pct): when 100% is crossed before the reset, and the level at reset."""
    if rate is None or reset_ts is None or reset_ts <= now:
        return None, None
    hours = (reset_ts - now) / 3600
    finish = pct + rate * hours
    if rate > 0 and finish >= 100:
        exhaust_ts = now + (100 - pct) / rate * 3600
        return int(exhaust_ts), 100.0
    return None, max(0.0, min(finish, 100.0))


def median_peak(peaks: list[float]) -> float | None:
    """Median consumption of complete windows, or None below the comparison floor."""
    if len(peaks) < MIN_COMPARE_WINDOWS:
        return None
    return float(median(peaks))


@dataclass(frozen=True)
class Runway:
    reset_ts: int | None
    remaining_s: int
    pct: float | None
    headroom_pct: float | None
    rate: float | None
    exhaust_ts: int | None
    finish_pct: float | None
    sustainable: float | None
    verdict: str
    comparison: str

    @property
    def critical(self) -> bool:
        return self.exhaust_ts is not None


def compute_runway(
    pct: float | None,
    rate: float | None,
    span_s: int,
    reset_ts: int | None,
    now: int,
    baseline: float | None = None,
) -> Runway:
    remaining = max(0, reset_ts - now) if reset_ts is not None else 0
    headroom = None if pct is None else max(0.0, 100.0 - pct)
    if pct is None:
        return Runway(
            reset_ts, remaining, None, None, rate, None, None, None, "No session readings yet.", ""
        )
    if reset_ts is None or remaining <= 0:
        verdict = "No session running. The next message starts a fresh session window."
        return Runway(reset_ts, 0, pct, headroom, rate, None, None, None, verdict, "")
    exhaust_ts, finish = project(pct, rate, now, reset_ts)
    sustain = sustainable_rate(headroom, remaining)
    left = f"{headroom:.0f}% left, resets in {fmt_span(remaining)}."
    if rate is None:
        verdict = f"Collecting: {fmt_span(span_s)} of samples. {left}"
        finish = pct
    elif abs(rate) <= FLAT_RATE_PTS_PER_HOUR:
        verdict = f"Flat for {fmt_span(span_s)}. {left}"
    elif rate < 0:
        verdict = f"Falling over the last {fmt_span(span_s)}. {left}"
    elif exhaust_ts is not None:
        verdict = (
            f"Exhausted at {clock(exhaust_ts)}, {fmt_span(reset_ts - exhaust_ts)} before reset."
        )
    else:
        verdict = f"At this rate you finish with {100 - (finish or pct):.0f}% unused."
    comparison = ""
    if baseline and finish is not None and rate is not None:
        ratio = finish / baseline
        comparison = f"On track for {ratio:.1f}× your median window ({baseline:.0f}%)."
    return Runway(
        reset_ts, remaining, pct, headroom, rate, exhaust_ts, finish, sustain, verdict, comparison
    )


# -- per-hour consumption strip ----------------------------------------------------


@dataclass(frozen=True)
class HourBar:
    index: int
    start_ts: int
    end_ts: int
    consumed: float | None  # points consumed in this hour; None when unknown
    state: str  # done | partial | future | nodata


def _consumed(rows: list[QuotaRow]) -> float | None:
    if len(rows) < MIN_BUCKET_SAMPLES:
        return None
    total = 0.0
    for segment in split_at_resets(rows):  # a reset inside the hour is not negative consumption
        total += segment[-1].pct - segment[0].pct
    return round(max(0.0, total), 1)


def hour_strip(rows: list[QuotaRow], window_start: int, now: int) -> list[HourBar]:
    """Five bars for the window starting at ``window_start``; unstarted hours are ``future``."""
    ordered = sorted(rows, key=lambda r: r.ts)
    bars = []
    for i in range(HOURS_PER_WINDOW):
        start = window_start + i * 3600
        end = start + 3600
        if start >= now:  # an hour that has not begun, including one starting this second
            bars.append(HourBar(i, start, end, None, "future"))
            continue
        inside = [r for r in ordered if start <= r.ts <= min(end, now)]
        # carry the last sample before the hour in, so the hour's first minute counts
        before = [r for r in ordered if r.ts < start]
        if before and inside and inside[0].ts - start > 0:
            inside = [before[-1], *inside]
        consumed = _consumed(inside)
        state = "nodata" if consumed is None else ("partial" if end > now else "done")
        bars.append(HourBar(i, start, end, consumed, state))
    return bars
