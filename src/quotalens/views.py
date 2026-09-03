"""View options: time range, hidden series, burn lookback, auto-refresh.

Parsed from the query string, so a view is a URL. Invalid input falls back to
the default silently: a bookmark with a typo should still render a dashboard.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from urllib.parse import urlencode

RANGE_PRESETS: dict[str, int] = {
    "15m": 15 * 60,
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 86400,
}
RANGE_KEYS = (*RANGE_PRESETS, "all")
LOOKBACKS: dict[str, int] = {"5m": 300, "15m": 900, "1h": 3600, "6h": 6 * 3600}
REFRESH: dict[str, int] = {"off": 0, "10s": 10, "30s": 30, "1m": 60, "5m": 300}
SORTS = ("consumed",)  # the default order, most recent first, has no key
COLLECTING_UNDER_S = 15 * 60  # less data than this: say "collecting" instead of a grid
MIN_CUSTOM_SPAN_S = 60
AUTO = "auto"

_CUSTOM_RE = re.compile(r"^(\d{9,11})-(\d{9,11})$")
_WINDOW_KEY_RE = re.compile(r"^[a-z0-9_:\-]{1,64}$")


@dataclass(frozen=True)
class ViewOptions:
    range_key: str = AUTO  # preset key, "all", "custom", or "auto"
    custom: tuple[int, int] | None = None  # (from_ts, to_ts) when range_key == "custom"
    hidden: frozenset[str] = frozenset()
    lookback_key: str | None = None  # None: the server default
    refresh_key: str | None = None  # None: the server default
    sort_key: str | None = None  # session history: None (recent) or "consumed"

    def lookback_s(self, default_s: int) -> int:
        return LOOKBACKS.get(self.lookback_key or "", default_s)

    def refresh_s(self, default_s: int) -> int:
        return REFRESH.get(self.refresh_key or "", default_s)

    def range_param(self) -> str | None:
        if self.range_key == AUTO:
            return None
        if self.range_key == "custom" and self.custom:
            return f"{self.custom[0]}-{self.custom[1]}"
        return self.range_key

    def query(self, **overrides: object) -> str:
        """Query string for this view with overrides applied; empty when all defaults."""
        opts = replace(self, **overrides) if overrides else self
        params: dict[str, str] = {}
        if (rng := opts.range_param()) is not None:
            params["range"] = rng
        if opts.hidden:
            params["hide"] = ",".join(sorted(opts.hidden))
        if opts.lookback_key:
            params["lookback"] = opts.lookback_key
        if opts.refresh_key:
            params["refresh"] = opts.refresh_key
        if opts.sort_key:
            params["sort"] = opts.sort_key
        return urlencode(params)

    def href(self, **overrides: object) -> str:
        q = self.query(**overrides)
        return f"/?{q}" if q else "/"

    def toggled(self, window: str) -> frozenset[str]:
        return self.hidden - {window} if window in self.hidden else self.hidden | {window}


def parse_view(params: Mapping[str, str], now: int) -> ViewOptions:
    range_key, custom = AUTO, None
    raw = (params.get("range") or "").strip()
    if raw in RANGE_KEYS:
        range_key = raw
    elif match := _CUSTOM_RE.match(raw):
        start, end = int(match.group(1)), int(match.group(2))
        end = min(end, now)
        if end - start >= MIN_CUSTOM_SPAN_S:
            range_key, custom = "custom", (start, end)
    hidden = frozenset(
        key for key in (params.get("hide") or "").split(",") if _WINDOW_KEY_RE.match(key)
    )
    lookback = params.get("lookback")
    refresh = params.get("refresh")
    sort = params.get("sort")
    return ViewOptions(
        range_key=range_key,
        custom=custom,
        hidden=hidden,
        lookback_key=lookback if lookback in LOOKBACKS else None,
        refresh_key=refresh if refresh in REFRESH else None,
        sort_key=sort if sort in SORTS else None,
    )


@dataclass(frozen=True)
class ResolvedRange:
    start: int
    end: int
    key: str  # preset key, "all", or "custom"
    label: str
    auto: bool  # chosen to fit the data rather than asked for
    collecting: bool  # too little data for a chart to mean anything
    data_span_s: int  # oldest sample to now


def resolve_range(
    opts: ViewOptions,
    oldest_ts: int | None,
    now: int,
    session: tuple[int, int] | None = None,
) -> ResolvedRange:
    """Pick the concrete window.

    Auto = the current 5-hour session window, start to reset, when one is running;
    otherwise the smallest preset that covers all the data.
    """
    data_span = max(0, now - oldest_ts) if oldest_ts is not None else 0
    if opts.range_key == AUTO and session is not None and session[1] > now:
        return ResolvedRange(
            session[0], session[1], "session", "this window", True, False, data_span
        )
    if opts.range_key == "custom" and opts.custom:
        start, end = opts.custom
        return ResolvedRange(
            start, end, "custom", _custom_label(start, end), False, False, data_span
        )
    if opts.range_key in RANGE_PRESETS:
        seconds = RANGE_PRESETS[opts.range_key]
        return ResolvedRange(
            now - seconds, now, opts.range_key, opts.range_key, False, False, data_span
        )
    if opts.range_key == "all":
        start = oldest_ts if oldest_ts is not None else now - RANGE_PRESETS["15m"]
        return ResolvedRange(start, now, "all", "all", False, False, data_span)
    # auto
    if oldest_ts is None or data_span < COLLECTING_UNDER_S:
        return ResolvedRange(now - RANGE_PRESETS["15m"], now, "15m", "15m", True, True, data_span)
    for key, seconds in RANGE_PRESETS.items():
        if seconds >= data_span:
            return ResolvedRange(now - seconds, now, key, key, True, False, data_span)
    return ResolvedRange(oldest_ts, now, "all", "all", True, False, data_span)


def _custom_label(start: int, end: int) -> str:
    from quotalens.dashboard import clock  # local import: dashboard imports this module

    return f"{clock(start)} to {clock(end)}"
