"""Build the dashboard view model from the store and the poller status.

Everything numeric or stateful is decided here; :mod:`quotalens.render` only
turns the model into markup. Reset logic is consumed from :mod:`quotalens.burn`,
never re-implemented.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import Any

from quotalens.alerts import ALERT_KIND, CLEARED_KIND, standing
from quotalens.burn import BurnResult, burn_rate, min_trusted_span, split_at_resets
from quotalens.config import Settings
from quotalens.parse import SpendReading, humanize
from quotalens.poller import PollerStatus
from quotalens.runway import HourBar, Runway, compute_runway, hour_strip, median_peak
from quotalens.sessions import (
    MODEL_VIOLATION_KIND,
    SESSION_LENGTH_S,
    SessionWindow,
    coverage_pct,
    idle_spans,
    window_from_row,
)
from quotalens.state import (
    CRITICAL,
    ELEVATED,
    NORMAL,
    OK,
    STALE_AFTER_INTERVALS,
    Epistemic,
    collector_state,
    magnitude_state,
    worst,
)
from quotalens.store import QuotaRow, Store
from quotalens.views import (
    LOOKBACKS,
    RANGE_KEYS,
    REFRESH,
    ResolvedRange,
    ViewOptions,
    resolve_range,
)

HERO_HOURS = 5
CHART_W, CHART_H = 1272, 216
CHART_L, CHART_R, CHART_T, CHART_B = 44, 122, 14, 20
PLOT_RIGHT = CHART_W - CHART_R  # the right edge of the plotting area, in chart units
HERO_W, HERO_H = 1272, 108
LABEL_GAP = 13.0
MAX_POINTS_PER_SERIES = 600  # longer ranges are bucketed, keeping the last sample per bucket
FORCE_NOTE_TTL_S = 15
HISTORY_ROWS = 20
SPARK_POINTS = 40
THIN_COVERAGE_PCT = 25.0  # under this coverage a window is a guess
PARTIAL_COVERAGE_PCT = 80.0  # under this the row says how much was observed
PEAK_FINAL_NOTE_PTS = 2.0  # peak and close differ by more than this: worth a note

RATE_WINDOW = "five_hour"
# Storage keys never change (bookmarks and stored rows depend on them); only names do.
DISPLAY_LABELS = {
    "five_hour": "Session",
    "seven_day": "Weekly — all models",
    "seven_day_sonnet": "Weekly — Sonnet",
    "seven_day_opus": "Weekly — Opus",
    "seven_day_oauth_apps": "Weekly — OAuth apps",
}
SHORT_LABELS = {"five_hour": "Session", "seven_day": "Weekly all"}

# Fable models may use "up to 50% of your weekly usage limits" at no extra cost,
# so this meter's 100% is half the weekly pool. Without saying so the dashboard
# reports an exhausted account when the account has half its week left.
SUBCAP_NOTE = "half of weekly pool"
SUBCAP_TITLE = (
    "Fable models may use up to 50% of your weekly limits. 100% here means that half is "
    "spent, not that the account is out of quota, so it does not set the account state."
)


def is_subcapped(window: str) -> bool:
    """Does this window measure a slice of the weekly pool rather than the pool?"""
    return window.startswith("limit:") and "fable" in window


def _model_name(window: str, stored_label: str | None) -> str:
    """The model behind a ``limit:`` key: the stored display name, else the slug."""
    if stored_label and not stored_label.startswith("Weekly"):
        return stored_label
    return humanize(window[6:]).title()


def display_label(window: str, stored_label: str | None) -> str:
    """The full name: a user never sees a raw key like ``limit:fable``."""
    if window in DISPLAY_LABELS:
        return DISPLAY_LABELS[window]
    if window.startswith("limit:"):
        return "Weekly — " + _model_name(window, stored_label)
    if window.startswith("unknown:"):
        return "Unlabelled " + humanize(window[8:])
    return stored_label or humanize(window)


def short_label(window: str, stored_label: str | None) -> str:
    """The compact name for chart end labels and table headers."""
    if window in SHORT_LABELS:
        return SHORT_LABELS[window]
    if window.startswith("limit:"):
        return "Weekly " + _model_name(window, stored_label)
    return display_label(window, stored_label)


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


def day_month(dt: datetime) -> str:
    """ "4 Sep", without a leading zero on any platform.

    ``%-d`` is a glibc and BSD extension and ``%#d`` is the Windows spelling;
    neither is portable, and asking for the wrong one raises ValueError rather
    than degrading. The day is an integer, so format it as one.
    """
    return f"{dt.day} {dt.strftime('%b')}"


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
    return day_month(dt)


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
    delta_text: str  # change over the selected range
    bar_pct: float  # clipped to the track
    withheld: bool
    note: str = ""  # a qualifier on what 100% means here
    note_title: str = ""  # the long form, on hover
    subcap: bool = False  # measures a slice of another window, so not an account-level state


@dataclass
class BurnView:
    """The hero: how much session is left and how long until it resets."""

    rate_text: str  # "4.82" or em dash; a supporting figure inside the verdict line
    unit: str
    why: str  # the verdict sentence (plus the median comparison)
    withheld: bool
    elevated: bool
    detail: str = ""  # quiet second line: rate, span, lookback, threshold
    runway: Runway | None = None
    hours: list[HourBar] = field(default_factory=list)
    hours_max: float = 20.0  # scale for the hour bars
    headroom_text: str = "—"  # the lit figure
    critical: bool = False  # exhausted before the reset at the current rate


@dataclass
class SeriesView:
    key: str
    label: str
    slot: int
    paths: list[str]
    end_x: float
    end_y: float
    label_y: float
    hidden: bool
    toggle_href: str


@dataclass
class ChartView:
    series: list[SeriesView]
    y_ticks: list[tuple[float, str]]
    x_ticks: list[tuple[float, str]]
    y_max: float
    has_data: bool
    gaps: list[tuple[float, float]]  # x spans where nothing was collected
    gap_minutes: int
    data_json: str  # for the hover readout
    collecting_text: str  # non-empty on cold start instead of a grid
    session_x: list[float] = field(default_factory=list)  # session window starts
    idle: list[tuple[float, float]] = field(default_factory=list)  # no window running
    idle_minutes: int = 0
    now_x: float = 0.0  # where "now" falls; beyond it the chart is the future
    future: bool = False
    hour_x: list[float] = field(default_factory=list)  # hourly separators in the current window
    projection: str = ""  # SVG path from now to the reset at the current rate
    projection_critical: bool = False
    cross: tuple[float, float, str] | None = None  # the 100% crossing, if before the reset


@dataclass
class SessionRowView:
    started_at: int
    ends_at: int
    window_text: str
    href: str
    peak_text: str
    note: str  # row title: peak versus close when they genuinely differ
    columns: list[tuple[str, str, bool]]  # per weekly limit: (delta, end, reset)
    samples: int
    coverage_pct: float
    badge: str  # "partial, 43% observed" when coverage is poor, else ""
    spark: str  # polyline points of the session trace inside the window, 60x18 box
    thin: bool  # too few samples to trust
    is_current: bool
    selected: bool  # the chart range is exactly this window


@dataclass
class HistoryView:
    rows: list[SessionRowView]
    headers: list[str]  # weekly-limit column labels
    sort: str  # recent | consumed
    sort_links: dict[str, str]
    total: int = 0  # windows on record
    show_all_href: str = ""  # non-empty when more rows exist than are shown
    show_less_href: str = ""  # non-empty when every row is shown and there is a first page


@dataclass
class SpendView:
    used_text: str | None
    limit_text: str | None
    pct: float | None
    pct_text: str
    bar_pct: float
    status_text: str
    state: str  # normal | critical (neutral while disabled)
    withheld: bool


@dataclass
class Control:
    key: str
    label: str
    href: str
    active: bool


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
    notes: list[str]  # transient notes (forced refresh suppressed, etc.)
    diagnostics: list[str]  # permanent, unactionable: side panel
    side: dict[str, str]
    footer: dict[str, str]
    refresh_s: int
    view: ViewOptions
    rng: ResolvedRange
    range_controls: list[Control]
    lookback_controls: list[Control]
    refresh_controls: list[Control]
    lookback_s: int
    history: HistoryView
    cooldown_s: int = 0  # seconds until another forced poll is allowed
    events: list[dict[str, Any]] = field(default_factory=list)
    alert_standing: bool = False  # a burn alert fired and has not cleared


# -- builders -------------------------------------------------------------------


def build_dashboard(
    settings: Settings,
    store: Store,
    status: PollerStatus,
    now: int,
    burn_alert: float,
    view: ViewOptions | None = None,
    cooldown_s: int = 0,
) -> Dashboard:
    view = view or ViewOptions()
    epistemic = collector_state(status, settings.poll_interval_s, now)
    withheld = epistemic.kind != OK
    lookback_s = view.lookback_s(settings.burn_lookback_min * 60)
    refresh_default = max(10, min(settings.poll_interval_s // 2, 30))
    refresh_s = view.refresh_s(refresh_default)

    latest = store.latest_quota()
    order = _window_order(status, latest)
    slots = assign_slots(order)
    oldest = store.oldest_ts()
    sessions_all = [window_from_row(r) for r in store.sessions(limit=500, order="recent")]
    current = next((w for w in sessions_all if w.is_current), None)
    session = (current.started_at, current.ends_at) if current else None
    rng = resolve_range(view, oldest, now, session)
    # Fetch a little before the range so the reset split and the burn lookback have context.
    fetch_from = min(rng.start, now - max(lookback_s * 4, HERO_HOURS * 3600))
    if current:
        fetch_from = min(fetch_from, current.started_at)
    all_rows = _series_by_window(store, fetch_from)
    range_rows = {
        w: [r for r in rows if rng.start <= r.ts <= min(rng.end, now)]
        for w, rows in all_rows.items()
    }

    burns = {w: burn_rate(w, all_rows.get(w, []), lookback_s, now) for w in order}
    rate_burn = burns.get(RATE_WINDOW)
    burn_elevated = bool(
        rate_burn
        and rate_burn.rate_pct_per_hour is not None
        and rate_burn.rate_pct_per_hour >= burn_alert
        and rate_burn.span_s >= min_trusted_span(lookback_s)
    )

    windows = [
        _window_view(
            row,
            slots[row.window],
            range_rows.get(row.window, []),
            rng,
            withheld,
            burn_elevated,
            now,
        )
        for row in sorted(latest, key=lambda r: slots[r.window])
    ]
    baseline = median_peak([w.peak_pct for w in sessions_all if not w.is_current])
    burn = _burn_view(
        rate_burn,
        next((r for r in latest if r.window == RATE_WINDOW), None),
        all_rows.get(RATE_WINDOW, []),
        lookback_s,
        withheld,
        burn_elevated,
        burn_alert,
        now,
        session,
        baseline,
    )
    gap_threshold = STALE_AFTER_INTERVALS * settings.poll_interval_s
    labels = {r.window: r.label for r in latest}
    prior_ts = max(
        (r.ts for rows in all_rows.values() for r in rows if r.ts < rng.start), default=None
    )
    if prior_ts is None and oldest is not None and oldest < rng.start:
        prior_ts = rng.start  # history reaches back past the range: the left edge counts
    chart = _chart_view(
        range_rows, slots, labels, rng, view, gap_threshold, now, withheld, prior_ts
    )
    _mark_sessions(chart, sessions_all, rng, now)
    _mark_runway(chart, burn.runway, session, rng, now, withheld)
    sort = view.sort_key or "recent"
    listed = (
        [w for w in sorted(sessions_all, key=lambda w: (-w.peak_pct, -w.started_at))]
        if (sort == "consumed")
        else sessions_all
    )
    page = listed if view.history_all else listed[:HISTORY_ROWS]
    history = _history_view(page, labels, slots, view, settings.poll_interval_s, now, store)
    history.total = len(sessions_all)
    if len(sessions_all) > HISTORY_ROWS:
        if view.history_all:
            history.show_less_href = view.href(history_all=False)
        else:
            history.show_all_href = view.href(history_all=True)
    spend = _spend_view(status.spend, withheld, now)

    # A sub-capped window is excluded: Fable at 100% is half the weekly pool spent,
    # and an account chip reading "critical" for that is simply false.
    account_states = [w.state for w in windows if not w.subcap]
    magnitude = worst(account_states + ([spend.state] if spend else []))
    if withheld:
        chip, chip_text = epistemic.kind, epistemic.title
    elif magnitude != NORMAL:
        chip, chip_text = magnitude, magnitude
    else:
        chip, chip_text = "", ""

    diagnostics = []
    violation = store.recent_events(limit=1, kind=MODEL_VIOLATION_KIND)
    if violation:
        diagnostics.append(violation[0].detail)
    if status.ignored_blocks:
        keys = ", ".join(b["key"] for b in status.ignored_blocks)
        diagnostics.append(f"Payload blocks without a reset time, not charted: {keys}.")
    notes = _transient_notes(status, now)

    events = [e.as_dict() for e in store.recent_events(limit=6)]
    alert_standing = _alert_standing(store)
    counts = store.counts()
    size = store.db_size_bytes()
    side = {
        "Range": rng.label + (" (auto)" if rng.auto else ""),
        "Not collected": f"{chart.gap_minutes} min in range",
        "No session": f"{chart.idle_minutes} min in range",
        "Poll interval": f"{settings.poll_interval_s}s",
        "Samples stored": f"{counts['quota']:,}",
        "Database": f"{size / 1_048_576:.1f} MB" if size is not None else "in memory",
        "Oldest sample": (f"{day_month(local(oldest))} {clock(oldest)}" if oldest else "none"),
        "Last poll": clock(status.last_attempt_ts) if status.last_attempt_ts else "never",
        "Next poll": clock(status.next_poll_ts) if status.next_poll_ts else "pending",
    }
    footer = {"bind": f"{settings.host}:{settings.port}", "db": str(store.path)}
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
        notes=notes,
        diagnostics=diagnostics,
        side=side,
        footer=footer,
        refresh_s=refresh_s,
        view=view,
        rng=rng,
        range_controls=[
            Control(k, k, view.href(range_key=k, custom=None), rng.key == k and not rng.auto)
            for k in RANGE_KEYS
        ],
        lookback_controls=[
            Control(k, k, view.href(lookback_key=k), LOOKBACKS[k] == lookback_s) for k in LOOKBACKS
        ],
        refresh_controls=[
            Control(k, k, view.href(refresh_key=k), REFRESH[k] == refresh_s) for k in REFRESH
        ],
        lookback_s=lookback_s,
        history=history,
        cooldown_s=cooldown_s,
        events=events,
        alert_standing=alert_standing,
    )


def _alert_standing(store: Store) -> bool:
    """True when the most recent threshold event was a crossing, not a clearing."""
    latest = store.recent_events(limit=1, kind=ALERT_KIND)
    cleared = store.recent_events(limit=1, kind=CLEARED_KIND)
    return standing(latest[0].ts if latest else None, cleared[0].ts if cleared else None)


def _transient_notes(status: PollerStatus, now: int) -> list[str]:
    note = status.extra.get("force_note") if status.extra else None
    if isinstance(note, dict) and now - int(note.get("ts", 0)) <= FORCE_NOTE_TTL_S:
        return [str(note.get("text", ""))]
    return []


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


def _change_over_range(rows: list[QuotaRow], rng: ResolvedRange) -> str:
    """Change of the window over the selected range, honest about resets in between."""
    if len(rows) < 2:
        return "no change data in range"
    segments = split_at_resets(rows)
    if len(segments) > 1:
        return f"{len(segments) - 1} reset{'s' if len(segments) > 2 else ''} in range"
    delta = rows[-1].pct - rows[0].pct
    return f"{delta:+.0f} pts in range"


def _window_view(
    row: QuotaRow,
    slot: int,
    rows_in_range: list[QuotaRow],
    rng: ResolvedRange,
    withheld: bool,
    burn_elevated: bool,
    now: int,
) -> WindowView:
    label = display_label(row.window, row.label)
    subcap = is_subcapped(row.window)
    note = SUBCAP_NOTE if subcap else ""
    note_title = SUBCAP_TITLE if subcap else ""
    if withheld:
        return WindowView(
            row.window,
            label,
            slot,
            None,
            "—",
            NORMAL,
            bool(row.is_active),
            f"last ok {clock(row.ts)}",
            "",
            0.0,
            True,
            note,
            note_title,
            subcap,
        )
    state = magnitude_state(row.pct, row.severity)
    if row.window == RATE_WINDOW and burn_elevated and state == NORMAL:
        state = ELEVATED
    return WindowView(
        row.window,
        label,
        slot,
        row.pct,
        fmt_pct(row.pct),
        state,
        bool(row.is_active),
        f"resets {when(parse_iso(row.resets_at), now)}",
        _change_over_range(rows_in_range, rng),
        max(0.0, min(row.pct, 100.0)),
        False,
        note,
        note_title,
        subcap,
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
    session: tuple[int, int] | None = None,
    baseline: float | None = None,
) -> BurnView:
    lookback_label = duration(lookback_s)
    if withheld:
        why = "Session state unknown while the collector is not reporting."
        return BurnView("—", "pts/hr", why, True, False)
    if burn is None or current is None:
        return BurnView("—", "pts/hr", "No session readings yet.", True, False)
    need_s = min_trusted_span(lookback_s)
    rate = burn.rate_pct_per_hour
    displayable = rate is not None and burn.span_s >= need_s
    reset_dt = parse_iso(current.resets_at)
    reset_ts = session[1] if session else (int(reset_dt.timestamp()) if reset_dt else None)
    runway = compute_runway(
        current.pct, rate if displayable else None, burn.span_s, reset_ts, now, baseline
    )
    why = runway.verdict + (f" {runway.comparison}" if runway.comparison else "")
    hours = hour_strip(rows, session[0], now) if session else []
    hours_max = max([b.consumed or 0.0 for b in hours] + [20.0])
    headroom = f"{runway.headroom_pct:.0f}" if runway.headroom_pct is not None else "—"
    if not displayable:
        detail = f"The {lookback_label} rate needs at least {need_s // 60} minutes of samples."
        return BurnView(
            "—", "pts/hr", why, False, False, detail, runway, hours, hours_max, headroom, False
        )
    text = f"{rate:.2f}" if abs(rate) < 100 else f"{rate:.0f}"
    detail = f"{text} pts/hr over the last {duration(burn.span_s)}, lookback {lookback_label}."
    if elevated:
        detail += f" Above the {alert:.0f} pts/hr alert threshold."
    return BurnView(
        text,
        "pts/hr",
        why,
        False,
        elevated,
        detail,
        runway,
        hours,
        hours_max,
        headroom,
        runway.critical,
    )


def find_gaps(
    timestamps: list[int],
    start: int,
    end: int,
    threshold_s: int,
    prior_ts: int | None = None,
) -> list[tuple[int, int]]:
    """Spans inside [start, end] longer than ``threshold_s`` with no sample.

    ``prior_ts`` anchors the left edge: the newest sample before the range, or the
    range start when history reaches back past it but no sample survives inside the
    fetch window. Without it a gap that *began* before the range is invisible, and a
    range that was two thirds uncollected reads as collected and flat — which is the
    one thing this tool exists not to do. Leading and trailing gaps both count.
    """
    inside = sorted(t for t in timestamps if start <= t <= end)
    marks = ([max(prior_ts, start)] if prior_ts is not None else []) + inside
    gaps: list[tuple[int, int]] = []
    for a, b in pairwise(marks):
        if b - a > threshold_s:
            gaps.append((a, b))
    if marks and end - marks[-1] > threshold_s:
        gaps.append((marks[-1], end))
    return gaps


def _bucket(rows: list[QuotaRow], bucket_s: int) -> list[QuotaRow]:
    """Keep the last sample per bucket. Called per reset-free segment, so resets survive."""
    if bucket_s <= 1:
        return rows
    kept: dict[int, QuotaRow] = {}
    for r in rows:
        kept[r.ts // bucket_s] = r
    return list(kept.values())


def _chart_view(
    series_rows: dict[str, list[QuotaRow]],
    slots: dict[str, int],
    labels: dict[str, str],
    rng: ResolvedRange,
    view: ViewOptions,
    gap_threshold_s: int,
    now: int,
    withheld: bool,
    prior_ts: int | None = None,
) -> ChartView:
    start, end = rng.start, rng.end
    span = max(1, end - start)
    plot_w = CHART_W - CHART_L - CHART_R
    plot_h = CHART_H - CHART_T - CHART_B
    visible = {w: rows for w, rows in series_rows.items() if w in slots and rows}
    max_pct = max((r.pct for rows in visible.values() for r in rows), default=0.0)
    y_max = max(100.0, math.ceil(max_pct / 25) * 25)

    def x_of(ts: int) -> float:
        return CHART_L + (ts - start) / span * plot_w

    def y_of(pct: float) -> float:
        return CHART_T + plot_h - (pct / y_max) * plot_h

    bucket_s = max(1, span // MAX_POINTS_PER_SERIES)
    series: list[SeriesView] = []
    data: list[dict[str, Any]] = []
    for window, rows in visible.items():
        hidden = window in view.hidden
        paths: list[str] = []
        pts_out: list[list[float]] = []
        for segment in split_at_resets(rows):
            seg = _bucket(segment, bucket_s)
            pts = [f"{x_of(r.ts):.1f} {y_of(r.pct):.1f}" for r in seg]
            if len(pts) == 1:
                pts.append(pts[0])
            paths.append("M" + " L".join(pts))
            pts_out.extend([r.ts, r.pct] for r in seg)
        last = rows[-1]
        series.append(
            SeriesView(
                window,
                short_label(window, labels.get(window)),
                slots[window],
                [] if hidden else paths,
                x_of(last.ts),
                y_of(last.pct),
                y_of(last.pct),
                hidden,
                view.href(hidden=view.toggled(window)),
            )
        )
        if not hidden:
            data.append(
                {"key": window, "label": series[-1].label, "slot": slots[window], "pts": pts_out}
            )  # the hover readout uses the same short name as the end label
    _spread_labels(series)
    series.sort(key=lambda s: -s.slot)  # draw the hero trace last, on top

    timestamps = sorted({r.ts for rows in visible.values() for r in rows})
    # the future is not a gap; the left edge is, when we should have been collecting
    gaps = find_gaps(timestamps, start, min(end, now), gap_threshold_s, prior_ts)
    gap_minutes = sum(b - a for a, b in gaps) // 60
    gap_x = [(x_of(a), x_of(b)) for a, b in gaps]

    step = 25 if y_max <= 150 else 50
    y_ticks = [(y_of(v), fmt_pct(v)) for v in range(0, int(y_max) + 1, step)]
    x_ticks = _x_ticks(start, end, x_of)
    collecting = ""
    if rng.collecting and not withheld:
        have = duration(rng.data_span_s) if rng.data_span_s else "0m"
        collecting = f"Collecting: {have} of data. The chart fills in as samples arrive."
    payload = {
        "start": start,
        "end": end,
        "l": CHART_L,
        "r": CHART_R,
        "w": CHART_W,
        "ymax": y_max,
        "series": data,
    }
    return ChartView(
        series,
        y_ticks,
        x_ticks,
        y_max,
        bool(series),
        gap_x,
        gap_minutes,
        json.dumps(payload, separators=(",", ":")),
        collecting,
    )


def _mark_runway(
    chart: ChartView,
    runway: Runway | None,
    session: tuple[int, int] | None,
    rng: ResolvedRange,
    now: int,
    withheld: bool,
) -> None:
    """The future: hourly separators in the current window and the projection to the reset."""
    start, end = rng.start, rng.end
    span = max(1, end - start)
    plot_w = CHART_W - CHART_L - CHART_R
    plot_h = CHART_H - CHART_T - CHART_B

    def x_of(ts: float) -> float:
        return CHART_L + (ts - start) / span * plot_w

    def y_of(pct: float) -> float:
        return CHART_T + plot_h - (pct / chart.y_max) * plot_h

    chart.now_x = x_of(min(now, end))
    chart.future = end > now
    if session:
        chart.hour_x = [
            x_of(session[0] + k * 3600) for k in range(1, 5) if start < session[0] + k * 3600 < end
        ]
    if withheld or runway is None or runway.rate is None or runway.reset_ts is None:
        return
    if runway.reset_ts <= now or runway.pct is None or runway.finish_pct is None:
        return
    target_ts = runway.exhaust_ts if runway.exhaust_ts else runway.reset_ts
    target_pct = 100.0 if runway.exhaust_ts else runway.finish_pct
    if target_ts <= now or target_ts > end + 1:
        return
    chart.projection = (
        f"M{x_of(now):.1f} {y_of(runway.pct):.1f} L{x_of(target_ts):.1f} {y_of(target_pct):.1f}"
    )
    chart.projection_critical = runway.exhaust_ts is not None
    if runway.exhaust_ts:
        chart.cross = (
            x_of(runway.exhaust_ts),
            y_of(100.0),
            f"exhausted {clock(runway.exhaust_ts)}",
        )


def _mark_sessions(
    chart: ChartView, windows: list[SessionWindow], rng: ResolvedRange, now: int
) -> None:
    """Session starts as vertical rules; spans with no window running as flat shading."""
    start, end = rng.start, rng.end
    span = max(1, end - start)
    plot_w = CHART_W - CHART_L - CHART_R

    def x_of(ts: int) -> float:
        return CHART_L + (ts - start) / span * plot_w

    ordered = sorted(windows, key=lambda w: w.started_at)
    chart.session_x = [x_of(w.started_at) for w in ordered if start < w.started_at < end]
    idle = []
    minutes = 0
    for a, b in idle_spans(ordered, now):
        a2, b2 = max(a, start), min(b, end)
        if b2 > a2:
            idle.append((x_of(a2), x_of(b2)))
            minutes += (b2 - a2) // 60
    chart.idle = idle
    chart.idle_minutes = minutes


def _window_text(w: SessionWindow, now: int) -> str:
    start, end = local(w.started_at), local(w.ends_at)
    day = "" if start.date() == local(now).date() else day_month(start) + " "
    return f"{day}{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


def delta_cell(w: SessionWindow, key: str) -> tuple[str, str, bool]:
    """(delta, end, reset): how much the limit moved in the window, and where it ended."""
    d = w.deltas.get(key)
    if d is None:
        return ("—", "", False)
    delta = d.end - d.start
    return (f"{delta:+.0f}", f"{fmt_pct(d.end)}%", d.reset)


def sparkline(rows: list[QuotaRow], started_at: int) -> str:
    """Polyline points for a 60x18 box: x is time across the 5-hour window, y is 0-100%."""
    if len(rows) < 2:
        return ""
    step = max(1, len(rows) // SPARK_POINTS)
    picked = rows[::step]
    if picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    return " ".join(
        f"{2 + (r.ts - started_at) / SESSION_LENGTH_S * 56:.1f},{17 - r.pct / 100 * 16:.1f}"
        for r in picked
    )


def _history_view(
    windows: list[SessionWindow],
    labels: dict[str, str],
    slots: dict[str, int],
    view: ViewOptions,
    interval_s: int,
    now: int,
    store: Store | None = None,
) -> HistoryView:
    keys = sorted(
        {k for w in windows for k in w.deltas if k.startswith("limit:")},
        key=lambda k: (slots.get(k, 99), k),
    )
    columns = ["seven_day", *keys]
    headers = ["Weekly all", *(short_label(k, labels.get(k)) for k in keys)]
    rows = []
    for w in windows:
        end = min(w.ends_at, now)
        elapsed = max(1, end - w.started_at)
        coverage = coverage_pct(w.covered_s, elapsed)
        note = ""
        if abs(w.peak_pct - w.final_pct) > PEAK_FINAL_NOTE_PTS:
            note = f"Peak {fmt_pct(w.peak_pct)}%, closed at {fmt_pct(w.final_pct)}%"
        spark = ""
        if store is not None:
            trace = store.quota_series(w.first_ts, window=RATE_WINDOW, until_ts=w.last_ts)
            spark = sparkline(trace, w.started_at)
        rows.append(
            SessionRowView(
                started_at=w.started_at,
                ends_at=w.ends_at,
                window_text=_window_text(w, now),
                href=view.href(range_key="custom", custom=(w.started_at, end)),
                peak_text=f"{fmt_pct(w.peak_pct)}%",
                note=note,
                columns=[delta_cell(w, k) for k in columns],
                samples=w.samples,
                coverage_pct=coverage,
                badge=(
                    f"partial, {coverage:.0f}% observed" if coverage < PARTIAL_COVERAGE_PCT else ""
                ),
                spark=spark,
                thin=coverage < THIN_COVERAGE_PCT,
                is_current=w.is_current,
                selected=view.custom == (w.started_at, end),
            )
        )
    sort = view.sort_key or "recent"
    return HistoryView(
        rows,
        headers,
        sort,
        {"recent": view.href(sort_key=None), "consumed": view.href(sort_key="consumed")},
    )


def _x_ticks(start: int, end: int, x_of: Any) -> list[tuple[float, str]]:
    span = end - start
    for step in (60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400, 172800):
        if span / step <= 7:
            break
    first = math.ceil(start / step) * step
    ticks = []
    t = first
    while t <= end:
        label = clock(t) if span <= 2 * 86400 else day_month(local(t))
        ticks.append((x_of(t), label))
        t += step
    return ticks


def _spread_labels(series: list[SeriesView]) -> None:
    ordered = sorted(series, key=lambda s: s.end_y)
    for prev, cur in pairwise(ordered):
        if cur.label_y - prev.label_y < LABEL_GAP:
            cur.label_y = prev.label_y + LABEL_GAP


def _spend_view(spend: SpendReading | None, withheld: bool, now: int) -> SpendView | None:
    """Neutral while extra usage is off (it cannot cost more); critical when on and over limit."""
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
    state = CRITICAL if spend.is_enabled and pct is not None and pct >= 100 else NORMAL
    if withheld:
        return SpendView(None, None, None, "—", 0.0, status, NORMAL, True)
    return SpendView(
        spend.used_text,
        spend.limit_text,
        pct,
        f"{pct:.0f}" if pct is not None else "—",
        max(0.0, min(pct or 0.0, 100.0)),
        status,
        state,
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
        "range": {
            "key": dash.rng.key,
            "start": dash.rng.start,
            "end": dash.rng.end,
            "auto": dash.rng.auto,
            "collecting": dash.rng.collecting,
        },
        "lookback_s": dash.lookback_s,
        "hidden": sorted(dash.view.hidden),
        "gap_minutes": dash.chart.gap_minutes,
        "windows": [
            {
                "key": w.key,
                "label": w.label,
                "slot": w.slot,
                "pct": w.pct,
                "state": w.state,
                "is_active": w.is_active,
                "withheld": w.withheld,
                "change": w.delta_text,
            }
            for w in dash.windows
        ],
        "burn": {
            "text": dash.burn.rate_text,
            "withheld": dash.burn.withheld,
            "why": dash.burn.why,
            "headroom": dash.burn.headroom_text,
            "critical": dash.burn.critical,
        },
        "runway": None
        if dash.burn.runway is None
        else {
            "reset_ts": dash.burn.runway.reset_ts,
            "remaining_s": dash.burn.runway.remaining_s,
            "headroom_pct": dash.burn.runway.headroom_pct,
            "exhaust_ts": dash.burn.runway.exhaust_ts,
            "finish_pct": dash.burn.runway.finish_pct,
            "sustainable": dash.burn.runway.sustainable,
            "verdict": dash.burn.runway.verdict,
            "comparison": dash.burn.runway.comparison,
            "hours": [
                {"start_ts": b.start_ts, "consumed": b.consumed, "state": b.state}
                for b in dash.burn.hours
            ],
        },
        "spend": None
        if dash.spend is None
        else {
            "used": dash.spend.used_text,
            "limit": dash.spend.limit_text,
            "pct": dash.spend.pct,
            "state": dash.spend.state,
            "status": dash.spend.status_text,
        },
        "notes": dash.notes,
        "diagnostics": dash.diagnostics,
        "events": dash.events,
        "alert_standing": dash.alert_standing,
        "sessions": [
            {
                "started_at": r.started_at,
                "ends_at": r.ends_at,
                "peak": r.peak_text,
                "note": r.note,
                "columns": [{"delta": d, "end": end, "reset": rs} for d, end, rs in r.columns],
                "samples": r.samples,
                "coverage_pct": r.coverage_pct,
                "badge": r.badge,
                "thin": r.thin,
                "is_current": r.is_current,
            }
            for r in dash.history.rows
        ],
        "idle_minutes": dash.chart.idle_minutes,
    }
