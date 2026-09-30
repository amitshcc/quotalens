"""The vendor's own split of this week's usage by surface, as the page and the API show it.

``parse.py`` reads the ``seven_day_breakdown`` block, ``store.py`` keeps a snapshot each
time a share changes. This module decides what of that is on screen and how it reads.

The shares are of *this week's usage* and sum to about 100: they are not shares of the
limit. "≈ N% of the week's limit" is share x Weekly-all percent / 100, rounded, and is
always labelled an estimate. Labels come from the payload; no surface is named here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from quotalens.budget import week_key
from quotalens.parse import parse_breakdown
from quotalens.store import StoredBreakdown

OTHER_KEY = "other"
# Non-amber series slots in tokens.css, in payload order (--s1 is the session window).
SERIES_SLOTS = ("--s2", "--s3", "--s4", "--s5", "--s6")
OTHER_COLOUR = "--txt-far"


@dataclass(frozen=True)
class SurfaceView:
    key: str
    label: str
    percent: float
    share_text: str  # "76%"
    limit_text: str | None  # "≈ 47%"; None when the weekly figure is not to be shown
    colour: str  # a CSS custom property name, e.g. "--s2"


@dataclass(frozen=True)
class SurfaceSection:
    rows: list[SurfaceView]
    as_of_ts: int  # epoch seconds of the vendor's own "as of" (else when we stored it)


def _ts(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def of_limit(percent: float, weekly_pct: float | None) -> int | None:
    """Whole percent of the weekly limit a share stands for; None without a weekly figure."""
    if weekly_pct is None:
        return None
    return round(percent * weekly_pct / 100)


def colours(keys: list[str]) -> list[str]:
    """One colour per surface: series slots in order, the muted token for ``other``."""
    out, slot = [], 0
    for key in keys:
        if key == OTHER_KEY:
            out.append(OTHER_COLOUR)
            continue
        out.append(SERIES_SLOTS[slot % len(SERIES_SLOTS)])
        slot += 1
    return out


def is_this_week(snapshot: StoredBreakdown, now: int) -> bool:
    """A split stored for an earlier week is not "where this week's quota went"."""
    started = _ts(snapshot.window_started_at)
    return started is not None and week_key(started) == week_key(now)


def _as_of(store: Any, snapshot: StoredBreakdown) -> int:
    """The vendor's ``as_of`` from the newest usage sample when it is the same week's block.

    A snapshot is only stored when a share changes, so its own timestamp can be hours older
    than the figure being current; the sample says how fresh the split really is.
    """
    rows = store.query("SELECT payload FROM sample WHERE source = 'usage' ORDER BY ts DESC LIMIT 1")
    if rows:
        try:
            found = parse_breakdown(json.loads(rows[0]["payload"]))
        except (ValueError, TypeError):
            found = None
        same_week = found is not None and found.window_started_at == snapshot.window_started_at
        if same_week and (as_of := _ts(found.as_of)) is not None:
            return as_of
    return snapshot.ts


def build_section(
    store: Any, now: int, weekly_pct: float | None, withheld: bool
) -> SurfaceSection | None:
    """The section's rows, or None to keep the pointer-only text (no split, stale, withheld)."""
    snapshot = store.latest_breakdown()
    if withheld or snapshot is None or not snapshot.rows or not is_this_week(snapshot, now):
        return None
    palette = colours([r.key for r in snapshot.rows])
    rows = []
    for share, colour in zip(snapshot.rows, palette, strict=True):
        est = of_limit(share.percent, weekly_pct)
        rows.append(
            SurfaceView(
                share.key,
                share.label,
                share.percent,
                f"{share.percent:.0f}%",
                None if est is None else f"≈ {est}%",
                colour,
            )
        )
    return SurfaceSection(rows, _as_of(store, snapshot))


def top_surface(snapshot: StoredBreakdown | None) -> dict[str, Any] | None:
    """The largest share of a week's split, as ``{"label", "percent"}``; None without one."""
    if snapshot is None or not snapshot.rows:
        return None
    top = max(snapshot.rows, key=lambda r: r.percent)
    return {"label": top.label, "percent": top.percent}


def mostly_by_week(store: Any) -> dict[str, dict[str, Any]]:
    """Top surface at each week's close, keyed by the Monday ISO date the Weeks table uses."""
    started = [
        r["window_started_at"]
        for r in store.query(
            "SELECT DISTINCT window_started_at FROM surface_share "
            "WHERE window_started_at IS NOT NULL"
        )
    ]
    out: dict[str, dict[str, Any]] = {}
    for window_started_at in started:
        ts = _ts(window_started_at)
        top = top_surface(store.breakdown_at_close(window_started_at))
        if ts is not None and top is not None:
            out[week_key(ts)] = top
    return out


def snapshot_dict(store: Any, now: int, weekly_pct: float | None) -> dict[str, Any] | None:
    """The newest stored split for ``/api/breakdown``; older weeks are labelled as such."""
    snapshot = store.latest_breakdown()
    if snapshot is None:
        return None
    current = is_this_week(snapshot, now)
    return {
        "window_started_at": snapshot.window_started_at,
        "as_of_ts": _as_of(store, snapshot),
        "is_current_week": current,
        "rows": [
            {
                "key": r.key,
                "label": r.label,
                "percent": r.percent,
                "of_limit": of_limit(r.percent, weekly_pct) if current else None,
            }
            for r in snapshot.rows
        ],
    }
