"""State rules for the dashboard, kept pure so they can be tested without HTML.

Two kinds of state, per DESIGN.md §5:

* **Magnitude** (normal, elevated, critical) colours a value. The API's own
  ``severity`` is the primary rule; numeric thresholds are the fallback for
  readings that carry none.
* **Epistemic** (stale, auth, unverified) colours the frame and removes the
  value. Stale is a watchdog on the last *successful* poll, not a reaction to
  the last error, so a collector that silently stops ticking surfaces too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from quotalens.poller import PollerStatus

ELEVATED_PCT = 75.0
CRITICAL_PCT = 90.0
STALE_AFTER_INTERVALS = 3
DEFAULT_BURN_ALERT_PTS_PER_HOUR = 20.0  # a rate that would empty a 5-hour window in 5 hours

NORMAL, ELEVATED, CRITICAL = "normal", "elevated", "critical"
OK, STALE, AUTH, UNVERIFIED = "ok", "stale", "auth", "unverified"


def magnitude_state(pct: float | None, severity: str | None) -> str:
    """The API's severity first; thresholds only when it says nothing."""
    if severity == "critical":
        return CRITICAL
    if severity == "warning":
        return ELEVATED
    if severity == "normal":
        return NORMAL
    if pct is None:
        return NORMAL
    if pct >= CRITICAL_PCT:
        return CRITICAL
    if pct >= ELEVATED_PCT:
        return ELEVATED
    return NORMAL


def worst(states: list[str]) -> str:
    order = {NORMAL: 0, ELEVATED: 1, CRITICAL: 2}
    return max(states, key=lambda s: order.get(s, 0)) if states else NORMAL


@dataclass(frozen=True)
class Epistemic:
    kind: str  # ok | stale | auth | unverified
    title: str  # chip text
    message: str  # what happened, in words that imply the action
    last_ok_ts: int | None


def _clock(ts: int | None) -> str:
    if ts is None:
        return "never"
    return datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")


def collector_state(status: PollerStatus, interval_s: int, now: int) -> Epistemic:
    """Every health condition gets its own wording, because each needs a different action."""
    last_ok = status.last_success_ts
    error = status.last_error or ""

    if status.state == "no_cookie":
        return Epistemic(
            AUTH, "no cookie", "No session cookie stored. Run `quotalens auth`.", last_ok
        )
    if status.state == "auth_expired":
        return Epistemic(
            AUTH,
            "auth failed",
            f"Cookie expired or rejected at {_clock(status.last_error_ts)}. Run `quotalens auth` "
            "with a fresh cookie. Quota is unknown, not zero.",
            last_ok,
        )
    if status.state == "error" and "parse_failed" in _last_kind(status):
        return Epistemic(
            UNVERIFIED,
            "unparsed",
            f"claude.ai answered but the response could not be parsed at "
            f"{_clock(status.last_error_ts)}. Run `quotalens probe` and open an issue with "
            "the output.",
            last_ok,
        )
    if status.generic_fallback:
        return Epistemic(
            UNVERIFIED,
            "shape drifted",
            "The response shape changed; readings were recovered by a generic fallback and "
            "are not trusted. Run `quotalens probe`.",
            last_ok,
        )
    if last_ok is None:
        return Epistemic(STALE, "no data", _never_message(status), None)
    if now - last_ok > STALE_AFTER_INTERVALS * interval_s:
        return Epistemic(STALE, "stale", _stale_message(status, last_ok), last_ok)
    return Epistemic(OK, "", _recent_condition(status, error), last_ok)


def _last_kind(status: PollerStatus) -> str:
    return status.extra.get("last_error_kind", "") if status.extra else ""


def _never_message(status: PollerStatus) -> str:
    if status.state == "starting":
        return "Waiting for the first poll."
    if status.state == "blocked":
        return "No successful poll yet: Cloudflare is blocking the client. See /api/health."
    if status.state == "rate_limited":
        return (
            f"No successful poll yet: rate limited, next attempt at {_clock(status.next_poll_ts)}."
        )
    return "No successful poll yet. " + (status.last_error or "")


def _stale_message(status: PollerStatus, last_ok: int) -> str:
    since = f"last good sample {_clock(last_ok)}"
    if status.state == "blocked":
        return f"Blocked by Cloudflare's bot challenge; {since}. See /api/health."
    if status.state == "rate_limited":
        return f"Rate limited by claude.ai; next attempt at {_clock(status.next_poll_ts)}; {since}."
    if status.state == "error":
        return f"claude.ai unreachable; {since}. Retrying with backoff."
    return f"Collector stopped ticking; {since}. Restart `quotalens serve`."


def _recent_condition(status: PollerStatus, error: str) -> str:
    if status.state == "rate_limited":
        return f"Rate limited; next attempt at {_clock(status.next_poll_ts)}."
    if status.state == "blocked":
        return "Last attempt was blocked by Cloudflare; showing the previous good sample."
    if status.state == "error" and error:
        return "Last attempt failed; showing the previous good sample."
    return ""
