"""Build the dashboard view model from the store and the poller status.

Everything numeric or stateful is decided here; :mod:`quotalens.render` only
turns the model into markup. Reset logic is consumed from :mod:`quotalens.burn`,
never re-implemented.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from quotalens.burn import BurnResult, burn_rate, split_at_resets
from quotalens.config import Settings
from quotalens.parse import SpendReading, humanize
from quotalens.poller import PollerStatus
from quotalens.state import (
    ELEVATED,
    OK,
    Epistemic,
    collector_state,
    magnitude_state,
    worst,
)
from quotalens.store import QuotaRow, Store

HERO_HOURS = 5
CHART_HOURS = 24
CHART_W, CHART_H = 1272, 216
CHART_L, CHART_R, CHART_T, CHART_B = 44, 122, 14, 20
HERO_W, HERO_H = 1272, 108
MAX_SLOTS = 6
LABEL_GAP = 13.0

RATE_WINDOW = "five_hour"
DISPLAY_LABELS = {
    "five_hour": "5-hour",
    "seven_day": "7-day",
    "seven_day_sonnet": "7-day Sonnet",
    "seven_day_opus": "7-day Opus",
}


def display_label(window: str, stored_label: str | None) -> str:
    """A user never sees a raw key like ``limit:fable`` where "Fable" belongs."""
    if window in DISPLAY_LABELS:
        return DISPLAY_LABELS[window]
    if window.startswith("limit:"):
        return stored_label or humanize(window[6:]).title()
    if window.startswith("unknown:"):
        return "Unlabelled " + humanize(window[8:])
    return stored_label or humanize(window)


def assign_slots(windows: list[str]) -> dict[str, int]:
    """Slot 1 is always five_hour, 2 seven_day, 3-5 model limits in order, 6 the rest."""
    slots: dict[str, int] = {}
    if RATE_WINDOW in windows:
        slots[RATE_WINDOW] = 1
    if "seven_day" in windows:
        slots["seven_day"] = 2
    nxt = 3
    for w in windows:
        if w in slots:
            continue
        if w.startswith("limit:") and nxt <= 5:
            slots[w] = nxt
            nxt += 1
        else:
            slots[w] = 6
    return slots


# -- time helpers ---------------------------------------------------------------


def local(ts: int) -> datetime:
    return datetime.fromtimestamp(ts).astimezone()


def clock(ts: int) -> str:
    return local(ts).strftime("%H:%M")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone() if dt.tzinfo else dt


def when(dt: datetime | None, now: int) -> str:
    """'17:00' today, else 'Fri 09:00', else '1 Oct'."""
    if dt is None:
        return "unknown"
    today = local(now).date()
    if dt.date() == today:
        return dt.strftime("%H:%M")
    if 0 < (dt.date() - today).days < 7:
        return dt.strftime("%a %H:%M")
    return dt.strftime("%-d %b")


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def fmt_pct(pct: float) -> str:
    return f"{pct:.0f}" if abs(pct - round(pct)) < 0.05 else f"{pct:.1f}"


# -- view model -----------------------------------------------------------------


@dataclass
class WindowView:
    key: str
    label: str
    slot: int
    pct: float | None  # None when the value is withheld
    pct_text: str
    state: str  # magnitude
    is_active: bool
    resets_text: str
    delta_text: str
    bar_pct: float  # clipped to the track
    withheld: bool


@dataclass
class BurnView:
    rate_text: str  # "4.82" or em dash
    unit: str
    why: str
    withheld: bool
    elevated: bool
    trace: str  # SVG path data, may be empty
    trace_ticks: list[tuple[float, str]] = field(default_factory=list)  # (x, label)
    alert_y: float | None = None


@dataclass
class SeriesView:
    key: str
    label: str
    slot: int
    paths: list[str]
    end_x: float
    end_y: float
    label_y: float


@dataclass
class ChartView:
    series: list[SeriesView]
    y_ticks: list[tuple[float, str]]
    x_ticks: list[tuple[float, str]]
    y_max: float
    has_data: bool


@dataclass
class SpendView:
    used_text: str | None
    limit_text: str | None
    pct: float | None
    pct_text: str
    bar_pct: float
    status_text: str
    withheld: bool


@dataclass
class Dashboard:
    now: int
    epistemic: Epistemic
    chip: str  # "" | elevated | critical | stale | auth | unverified
    chip_text: str
    windows: list[WindowView]
    burn: BurnView
    chart: ChartView
    spend: SpendView | None
    polled_text: str
    last_success_ts: int | None
    health_message: str
    diagnostics: list[str]
    side: dict[str, str]
    footer: dict[str, str]
    refresh_s: int


# -- builders -------------------------------------------------------------------


def build_dashboard(
    settings: Settings,
    store: Store,
    status: PollerStatus,
    now: int,
    burn_alert: float,
) -> Dashboard:
    epistemic = collector_state(status, settings.poll_interval_s, now)
    withheld = epistemic.kind != OK
    lookback_s = settings.burn_lookback_min * 60

    latest = store.latest_quota()
    order = _window_order(status, latest)
    slots = assign_slots(order)
    series_rows = _series_by_window(store, now - CHART_HOURS * 3600)

    burns = {w: burn_rate(w, series_rows.get(w, []), lookback_s, now) for w in order}
    rate_burn = burns.get(RATE_WINDOW)
    burn_elevated = bool(
        rate_burn
        and rate_burn.rate_pct_per_hour is not None
        and rate_burn.rate_pct_per_hour >= burn_alert
    )

    windows = [
        _window_view(row, slots[row.window], burns[row.window], withheld, burn_elevated, now)
        for row in sorted(latest, key=lambda r: slots[r.window])
    ]
    burn = _burn_view(
        rate_burn,
        next((r for r in latest if r.window == RATE_WINDOW), None),
        series_rows.get(RATE_WINDOW, []),
        lookback_s,
        withheld,
        burn_elevated,
        burn_alert,
        now,
    )
    chart = _chart_view(series_rows, slots, {r.window: r.label for r in latest}, now)
    spend = _spend_view(status.spend, withheld, now)

    magnitude = worst([w.state for w in windows])
    if withheld:
        chip, chip_text = epistemic.kind, epistemic.title
    elif magnitude != "normal":
        chip, chip_text = magnitude, magnitude
    else:
        chip, chip_text = "", ""

    diagnostics = []
    if status.ignored_blocks:
        keys = ", ".join(b["key"] for b in status.ignored_blocks)
        diagnostics.append(f"Payload blocks without a reset time, not charted: {keys}.")

    counts = store.counts()
    oldest = store.oldest_ts()
    size = store.db_size_bytes()
    side = {
        "Poll interval": f"{settings.poll_interval_s}s",
        "Samples stored": f"{counts['quota']:,}",
        "Database": f"{size / 1_048_576:.1f} MB" if size is not None else "in memory",
        "Oldest sample": local(oldest).strftime("%-d %b") if oldest else "none",
        "Last poll": clock(status.last_attempt_ts) if status.last_attempt_ts else "never",
        "Next poll": clock(status.next_poll_ts) if status.next_poll_ts else "pending",
    }
    footer = {
        "bind": f"{settings.host}:{settings.port}",
        "db": str(store.path),
    }
    return Dashboard(
        now=now,
        epistemic=epistemic,
        chip=chip,
        chip_text=chip_text,
        windows=windows,
        burn=burn,
        chart=chart,
        spend=spend,
        polled_text=_polled_text(status.last_success_ts, now),
        last_success_ts=status.last_success_ts,
        health_message=epistemic.message,
        diagnostics=diagnostics,
        side=side,
        footer=footer,
        refresh_s=max(10, min(settings.poll_interval_s // 2, 30)),
    )


def _window_order(status: PollerStatus, latest: list[QuotaRow]) -> list[str]:
    seen = [w for w in status.last_windows if any(r.window == w for r in latest)]
    for row in latest:
        if row.window not in seen:
            seen.append(row.window)
    return seen


def _series_by_window(store: Store, since: int) -> dict[str, list[QuotaRow]]:
    out: dict[str, list[QuotaRow]] = {}
    for row in store.quota_series(since):
        out.setdefault(row.window, []).append(row)
    return out


def _polled_text(last_ok: int | None, now: int) -> str:
    if last_ok is None:
        return "never polled"
    age = now - last_ok
    return f"polled {age}s ago" if age < 120 else f"last ok {clock(last_ok)}"


def _window_view(
    row: QuotaRow, slot: int, burn: BurnResult, withheld: bool, burn_elevated: bool, now: int
) -> WindowView:
    state = magnitude_state(row.pct, row.severity)
    if row.window == RATE_WINDOW and burn_elevated and state == "normal":
        state = ELEVATED
    reset_dt = parse_iso(row.resets_at)
    resets_text = f"resets {when(reset_dt, now)}"
    if burn.rate_pct_per_hour is not None and burn.from_pct is not None and burn.to_pct is not None:
        delta = burn.to_pct - burn.from_pct
        delta_text = f"{delta:+.0f} pts in {duration(burn.span_s)}"
    else:
        delta_text = "no rate yet"
    if withheld:
        return WindowView(
            row.window,
            display_label(row.window, row.label),
            slot,
            None,
            "—",
            "normal",
            bool(row.is_active),
            f"last ok {clock(row.ts)}",
            "",
            0.0,
            True,
        )
    return WindowView(
        row.window,
        display_label(row.window, row.label),
        slot,
        row.pct,
        fmt_pct(row.pct),
        state,
        bool(row.is_active),
        resets_text,
        delta_text,
        max(0.0, min(row.pct, 100.0)),
        False,
    )


def _burn_view(
    burn: BurnResult | None,
    current: QuotaRow | None,
    rows: list[QuotaRow],
    lookback_s: int,
    withheld: bool,
    elevated: bool,
    alert: float,
    now: int,
) -> BurnView:
    trace, ticks, alert_y = _hero_trace(rows, lookback_s, alert, now)
    if withheld or burn is None or current is None:
        why = (
            "Rate unknown while the collector is not reporting."
            if withheld
            else ("No 5-hour readings yet.")
        )
        return BurnView("—", "pts/hr", why, True, False, trace, ticks, alert_y)
    rate = burn.rate_pct_per_hour
    if rate is None:
        return BurnView(
            "—",
            "pts/hr",
            f"Not enough samples yet: {burn.reason}.",
            True,
            False,
            trace,
            ticks,
            alert_y,
        )
    text = f"{rate:.2f}" if abs(rate) < 100 else f"{rate:.0f}"
    reset_dt = parse_iso(current.resets_at)
    lookback_text = duration(burn.span_s)
    if rate > 0.05:
        hours_left = (100 - current.pct) / rate
        eta = local(now) + timedelta(hours=hours_left)
        if reset_dt is not None and eta >= reset_dt:
            at_reset = current.pct + rate * max(0.0, (reset_dt - local(now)).total_seconds() / 3600)
            why = (
                f"Over the last {lookback_text}. At this rate the 5-hour window reaches "
                f"about {fmt_pct(min(at_reset, 100))}% by its reset at {when(reset_dt, now)}."
            )
        else:
            why = (
                f"Over the last {lookback_text}. At this rate the 5-hour window is exhausted "
                f"at {when(eta, now)}, in {duration(hours_left * 3600)}"
                + (f", before it resets at {when(reset_dt, now)}." if reset_dt else ".")
            )
    elif rate < -0.05:
        why = f"Falling over the last {lookback_text}; the window is draining faster than use."
    else:
        why = f"Flat over the last {lookback_text}. Nothing is consuming the 5-hour window."
    if elevated:
        why += f" Above the {alert:.0f} pts/hr alert threshold."
    return BurnView(text, "pts/hr", why, False, elevated, trace, ticks, alert_y)


def _hero_trace(
    rows: list[QuotaRow], lookback_s: int, alert: float, now: int
) -> tuple[str, list[tuple[float, str]], float | None]:
    """Rolling burn rate over the last five hours, computed by calling burn_rate at each sample."""
    start = now - HERO_HOURS * 3600
    recent = [r for r in rows if r.ts >= start - lookback_s]
    points: list[tuple[int, float | None]] = []
    for i, row in enumerate(recent):
        if row.ts < start:
            continue
        result = burn_rate(row.window, recent[: i + 1], lookback_s, row.ts)
        points.append((row.ts, result.rate_pct_per_hour))
    ticks = [(HERO_W * (h / HERO_HOURS), clock(start + h * 3600)) for h in range(0, HERO_HOURS + 1)]
    rates = [p[1] for p in points if p[1] is not None]
    if not rates:
        return "", ticks, None
    y_max = max(max(rates), alert, 1.0) * 1.1
    y_min = min(min(rates), 0.0)
    span = (y_max - y_min) or 1.0
    top, bottom = 8.0, HERO_H - 4.0

    def y_of(v: float) -> float:
        return bottom - (v - y_min) / span * (bottom - top)

    segments: list[str] = []
    current: list[str] = []
    for ts, rate in points:
        if rate is None:
            if current:
                segments.append(" ".join(current))
                current = []
            continue
        x = (ts - start) / (HERO_HOURS * 3600) * HERO_W
        current.append(f"{'M' if not current else 'L'}{x:.1f} {y_of(rate):.1f}")
    if current:
        segments.append(" ".join(current))
    return " ".join(segments), ticks, y_of(alert)


def _chart_view(
    series_rows: dict[str, list[QuotaRow]],
    slots: dict[str, int],
    labels: dict[str, str],
    now: int,
) -> ChartView:
    start = now - CHART_HOURS * 3600
    plot_w = CHART_W - CHART_L - CHART_R
    plot_h = CHART_H - CHART_T - CHART_B
    max_pct = max((r.pct for rows in series_rows.values() for r in rows), default=0.0)
    y_max = max(100.0, math.ceil(max_pct / 25) * 25)

    def x_of(ts: int) -> float:
        return CHART_L + (ts - start) / (CHART_HOURS * 3600) * plot_w

    def y_of(pct: float) -> float:
        return CHART_T + plot_h - (pct / y_max) * plot_h

    series: list[SeriesView] = []
    for window, rows in series_rows.items():
        if window not in slots:
            continue
        paths = []
        for segment in split_at_resets(rows):
            pts = [f"{x_of(r.ts):.1f} {y_of(r.pct):.1f}" for r in segment]
            if len(pts) == 1:
                pts.append(pts[0])
            paths.append("M" + " L".join(pts))
        last = rows[-1]
        series.append(
            SeriesView(
                window,
                display_label(window, labels.get(window)),
                slots[window],
                paths,
                x_of(last.ts),
                y_of(last.pct),
                y_of(last.pct),
            )
        )
    _spread_labels(series)
    series.sort(key=lambda s: -s.slot)  # draw the hero trace last, on top
    step = 25 if y_max <= 150 else 50
    y_ticks = [(y_of(v), fmt_pct(v)) for v in range(0, int(y_max) + 1, step)]
    x_ticks = [(x_of(start + h * 3600), clock(start + h * 3600)) for h in range(0, 25, 4)]
    return ChartView(series, y_ticks, x_ticks, y_max, bool(series))


def _spread_labels(series: list[SeriesView]) -> None:
    ordered = sorted(series, key=lambda s: s.end_y)
    for prev, cur in pairwise(ordered):
        if cur.label_y - prev.label_y < LABEL_GAP:
            cur.label_y = prev.label_y + LABEL_GAP


def _spend_view(spend: SpendReading | None, withheld: bool, now: int) -> SpendView | None:
    if spend is None:
        return None
    if spend.is_enabled is False:
        until = parse_iso(spend.disabled_until)
        status = "Extra usage off" + (f" until {when(until, now)}" if until else "")
        if spend.spend_limit_reached:
            status += ", limit reached"
    elif spend.is_enabled:
        status = "Extra usage on"
    else:
        status = ""
    if spend.conflict:
        status = (status + ". " if status else "") + "Sources disagree on amounts; figures hidden."
    pct = spend.pct
    if withheld:
        return SpendView(None, None, None, "—", 0.0, status, True)
    return SpendView(
        spend.used_text,
        spend.limit_text,
        pct,
        f"{pct:.0f}" if pct is not None else "—",
        max(0.0, min(pct or 0.0, 100.0)),
        status,
        False,
    )


def as_json(dash: Dashboard) -> dict[str, Any]:
    """A compact JSON form of the model, for /api/dashboard."""
    return {
        "now_ts": dash.now,
        "collector": {
            "kind": dash.epistemic.kind,
            "title": dash.epistemic.title,
            "message": dash.epistemic.message,
            "last_success_ts": dash.last_success_ts,
        },
        "chip": dash.chip,
        "windows": [
            {
                "key": w.key,
                "label": w.label,
                "slot": w.slot,
                "pct": w.pct,
                "state": w.state,
                "is_active": w.is_active,
                "withheld": w.withheld,
            }
            for w in dash.windows
        ],
        "burn": {"text": dash.burn.rate_text, "withheld": dash.burn.withheld, "why": dash.burn.why},
        "spend": None
        if dash.spend is None
        else {
            "used": dash.spend.used_text,
            "limit": dash.spend.limit_text,
            "pct": dash.spend.pct,
            "status": dash.spend.status_text,
        },
        "diagnostics": dash.diagnostics,
    }
