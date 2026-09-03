"""Burn-rate threshold detection and the optional webhook.

You look at a dashboard when you already suspect something. The scenario this
project exists for — a background agent burning quota overnight — is the one
where nobody is looking, so the crossing has to push.

Detection is an edge, not a level: one event when the rate crosses the threshold
upward and one when it falls back, with hysteresis so a rate sitting on the line
does not chatter. The state lives in the poller, so a restart cannot re-fire an
alert that already fired.

The webhook is opt-in, one POST, no retry, and carries no account identifier and
no cookie. It feeds ntfy, Discord, Slack, Pushover or Home Assistant with one
code path, which desktop notifications could not: a systemd user unit without a
session bus has nowhere to put a notification.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

ALERT_KIND = "burn_alert"
CLEARED_KIND = "burn_cleared"
HYSTERESIS = 0.9  # fall back below 90% of the threshold before clearing
WEBHOOK_TIMEOUT_S = 5.0


def standing(latest_alert_ts: int | None, latest_cleared_ts: int | None) -> bool:
    """Is an alert still standing, judged from what was recorded before we started?

    The detector is in memory, so without this a restart re-crosses a threshold it
    never left and POSTs the webhook again.
    """
    if latest_alert_ts is None:
        return False
    return latest_cleared_ts is None or latest_cleared_ts < latest_alert_ts


@dataclass
class ThresholdDetector:
    """Edge detection for one threshold. Pure, so the transitions are testable."""

    threshold: float
    firing: bool = False

    def update(self, rate: float | None) -> str | None:
        """Feed a rate; get ``ALERT_KIND``, ``CLEARED_KIND`` or nothing."""
        if rate is None:
            return None
        if not self.firing and rate >= self.threshold:
            self.firing = True
            return ALERT_KIND
        if self.firing and rate < self.threshold * HYSTERESIS:
            self.firing = False
            return CLEARED_KIND
        return None


def describe(kind: str, rate: float, threshold: float, headroom_pct: float | None) -> str:
    """The sentence stored in the event row and shown on the dashboard."""
    left = f", {headroom_pct:.0f}% of the session left" if headroom_pct is not None else ""
    if kind == ALERT_KIND:
        return f"Burn rate {rate:.1f} pts/hr crossed the {threshold:.0f} pts/hr threshold{left}."
    return f"Burn rate fell back to {rate:.1f} pts/hr, below {threshold:.0f} pts/hr{left}."


def payload(
    kind: str,
    ts: int,
    rate: float,
    threshold: float,
    headroom_pct: float | None,
    resets_at: str | None,
    dashboard_url: str,
    profile: str = "",
) -> dict[str, Any]:
    """The webhook body.

    Carries no organisation id, no cookie and no account identifier: it leaves the
    machine, and the receiver only needs to know that a threshold moved and where to
    look. The profile name is a local label the user chose, not an account identifier,
    and without it a receiver watching two profiles can only tell them apart by port.
    """
    return {
        "event": kind,
        "profile": profile or "default",
        "ts": ts,
        "rate_pts_per_hour": round(rate, 2),
        "threshold_pts_per_hour": threshold,
        "headroom_pct": None if headroom_pct is None else round(headroom_pct, 1),
        "session_resets_at": resets_at,
        "text": describe(kind, rate, threshold, headroom_pct),
        "url": dashboard_url,
    }


async def post_webhook(
    url: str, body: dict[str, Any], timeout_s: float = WEBHOOK_TIMEOUT_S
) -> bool:
    """One POST, no retry. A failure is logged and dropped, never raised."""
    from curl_cffi.requests import AsyncSession  # the client we already ship

    try:
        async with AsyncSession() as session:
            response = await session.post(
                url,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                timeout=timeout_s,
            )
    except Exception as exc:  # a webhook must never be able to stop the collector
        log.warning("webhook POST failed: %s", type(exc).__name__)
        return False
    if response.status_code >= 400:
        log.warning("webhook POST returned %d", response.status_code)
        return False
    return True
