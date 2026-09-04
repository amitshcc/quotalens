"""Prometheus text exposition, hand written.

No ``prometheus_client``: five gauges rendered by hand keeps the dependency list
where it is. The format is small but exact, and a subtly malformed exposition is
worse than none, because Prometheus will scrape it and lie. So:

* one ``# HELP`` and one ``# TYPE`` per family, before that family's samples,
  and never repeated;
* label values escape backslash, double quote and newline; help text escapes
  backslash and newline (a quote is legal there);
* ``NaN`` for a value we do not have, never a zero standing in for unknown;
* exactly one trailing newline.

Loopback only, like everything else here: run Prometheus on the same host or
put it behind a proxy you already trust. There is no ``--host``.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from quotalens import __version__

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
PREFIX = "quotalens_"


def escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def format_value(value: float | None) -> str:
    """Prometheus wants a number; an absent reading is NaN, not zero."""
    if value is None:
        return "NaN"
    if value != value:  # already NaN
        return "NaN"
    if value in (float("inf"), float("-inf")):
        return "+Inf" if value > 0 else "-Inf"
    if isinstance(value, bool):
        return "1" if value else "0"
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


@dataclass
class Family:
    name: str  # without the prefix
    kind: str  # gauge | counter
    help: str
    samples: list[tuple[dict[str, str], float | None]] = field(default_factory=list)

    def add(self, value: float | None, **labels: str) -> None:
        self.samples.append((labels, value))

    def render(self) -> Iterator[str]:
        full = PREFIX + self.name
        yield f"# HELP {full} {escape_help(self.help)}"
        yield f"# TYPE {full} {self.kind}"
        for labels, value in self.samples:
            rendered = ",".join(f'{k}="{escape_label(v)}"' for k, v in sorted(labels.items()))
            suffix = f"{{{rendered}}}" if rendered else ""
            yield f"{full}{suffix} {format_value(value)}"


def render(families: Iterable[Family]) -> str:
    """The whole exposition, ending in exactly one newline."""
    lines: list[str] = []
    seen: set[str] = set()
    for family in families:
        if family.name in seen:
            raise ValueError(f"duplicate metric family {family.name!r}")
        seen.add(family.name)
        lines.extend(family.render())
    return "\n".join(lines) + "\n"


def collect(settings: object, store: object, status: object, now: int) -> list[Family]:
    """Every family, in a stable order. Imports stay local to keep this module thin."""
    from quotalens.budget import compute_budgets
    from quotalens.burn import burn_rate
    from quotalens.dashboard import RATE_WINDOW, parse_iso, weekly_limits
    from quotalens.sessions import RATE_WINDOW as SESSION_WINDOW
    from quotalens.sessions import window_from_row
    from quotalens.state import STALE_AFTER_INTERVALS

    interval = getattr(settings, "poll_interval_s", 60)
    last_ok = getattr(status, "last_success_ts", None)
    fresh = last_ok is not None and now - last_ok <= STALE_AFTER_INTERVALS * interval

    build = Family("build_info", "gauge", "Build information; the value is always 1.")
    build.add(1, version=__version__, profile=getattr(settings, "profile", "") or "default")

    up = Family("up", "gauge", "1 when the collector polled successfully within three intervals.")
    up.add(1 if fresh else 0)

    quota = Family("quota_percent", "gauge", "Percentage of a quota window consumed.")
    resets = Family("window_resets_at_seconds", "gauge", "Unix time at which a window resets.")
    for row in store.latest_quota():
        labels = {"window": row.window, "label": row.label or row.window}
        quota.add(row.pct if fresh else None, **labels)
        reset_dt = parse_iso(row.resets_at)
        resets.add(reset_dt.timestamp() if reset_dt else None, window=row.window)

    burn = Family("burn_pts_per_hour", "gauge", "Session burn rate in percentage points per hour.")
    lookback_s = getattr(settings, "burn_lookback_min", 15) * 60
    rows = store.quota_series(now - lookback_s * 4, window=SESSION_WINDOW)
    rate = burn_rate(SESSION_WINDOW, rows, lookback_s, now).rate_pct_per_hour
    burn.add(rate if fresh else None)

    threshold = Family(
        "burn_alert_threshold_pts_per_hour", "gauge", "Configured burn-rate alert threshold."
    )
    threshold.add(getattr(settings, "burn_alert_pts_per_hour", None))

    headroom = Family("session_headroom_percent", "gauge", "Percentage of the session window left.")
    session = next((r for r in store.latest_quota() if r.window == RATE_WINDOW), None)
    headroom.add(max(0.0, 100.0 - session.pct) if session and fresh else None)

    # The weekly limit in the unit the work comes in. NaN, never zero, when there
    # is not enough history to estimate the cost of a window: a zero here would
    # page someone at 3am for a budget that is merely unknown.
    windows_left = Family(
        "weekly_windows_remaining",
        "gauge",
        "Session windows the weekly headroom will pay for. basis=full is a window "
        "run to 100%, basis=typical is the median window in this history.",
    )
    window_cost = Family(
        "weekly_window_cost_points",
        "gauge",
        "Median weekly points a session window run to 100% has cost.",
    )
    clock_left = Family(
        "weekly_clock_windows_remaining",
        "gauge",
        "Five-hour windows of wall clock before the weekly limit resets.",
    )
    sessions = [window_from_row(r) for r in store.sessions(limit=500, order="recent")]
    report = compute_budgets(weekly_limits(store.latest_quota(), not fresh), sessions, now)
    for item in report.budgets:
        windows_left.add(item.full_windows, window=item.key, basis="full")
        windows_left.add(item.typical_windows, window=item.key, basis="typical")
        window_cost.add(item.cost_per_full, window=item.key)
        clock_left.add(item.clock_windows, window=item.key)

    polls_ok = Family("poll_success_total", "counter", "Successful polls since start.")
    polls_ok.add(getattr(status, "polls_ok", 0))
    polls_failed = Family("poll_failure_total", "counter", "Failed polls since start.")
    polls_failed.add(getattr(status, "polls_failed", 0))

    last = Family(
        "last_success_timestamp_seconds", "gauge", "Unix time of the last successful poll."
    )
    last.add(last_ok)

    counts = store.counts()
    rows_family = Family("rows", "gauge", "Rows held in each table.")
    for table in sorted(counts):
        rows_family.add(counts[table], table=table)

    spend = Family("spend_used_minor", "gauge", "Extra usage spent, in minor currency units.")
    limit = Family("spend_limit_minor", "gauge", "Extra usage limit, in minor currency units.")
    overage = store.latest_overage()
    currency = (overage or {}).get("currency", "") or ""
    spend.add((overage or {}).get("spent_minor"), currency=currency)
    limit.add((overage or {}).get("cap_minor"), currency=currency)

    return [
        build,
        up,
        quota,
        resets,
        burn,
        threshold,
        headroom,
        windows_left,
        window_cost,
        clock_left,
        polls_ok,
        polls_failed,
        last,
        rows_family,
        spend,
        limit,
    ]
