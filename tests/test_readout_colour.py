"""The headroom readout's colour is a magnitude state, not the readout's identity.

It used to be `--lit` unconditionally, with one override to red. So "89% left" —
the healthiest thing this tool can report — was amber, for the same reason "9%
left" was amber: the element was always amber. The colour carried no information,
and the one time it did change it was easy to miss, because the eye had learnt
that the number is always coloured.

The red came from `Runway.critical`, a *projection* (will this rate exhaust the
window before it resets), while the meter directly beneath was coloured by
`magnitude_state`, a *level*. Two adjacent elements, one palette, two meanings.

Now both read one tier, and these tests hold that: the readout is achromatic when
there is nothing to say, and it can never disagree with the meter about the window
they both describe.
"""

from __future__ import annotations

import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quotalens.api import create_app
from quotalens.config import settings_from_env
from quotalens.parse import QuotaReading
from quotalens.secrets import MemorySecretStore
from quotalens.store import Store

MINUTES = 20


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def render(
    pct: float,
    *,
    severity: str | None = None,
    reset_in_s: int = 3 * 3600,
    climb: float = 0.0,
    last_ok_offset: int = 0,
    burn_alert: float = 20.0,
):
    """A session at ``pct`` used, optionally climbing, rendered through the whole app."""
    now = int(time.time())
    settings = settings_from_env().with_overrides(
        db_path=Path(tempfile.mkdtemp()) / "t.db", burn_alert_pts_per_hour=burn_alert
    )
    store = Store(settings.db_path)
    reset = iso(now + reset_in_s) if reset_in_s else None
    for i in range(MINUTES):
        value = max(0.0, pct - climb * (MINUTES - 1 - i) / 60)
        store.record_quota(
            now - (MINUTES - 1 - i) * 60,
            [QuotaReading("five_hour", "5-hour", value, reset, severity, True)],
        )
    app = create_app(settings, store, MemorySecretStore("cookie"))
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now - last_ok_offset
    with TestClient(app) as client:
        return client.get("/").text, client.get("/api/dashboard").json()


def readout_class(html: str) -> str:
    return re.search(r'<div class="(readout[^"]*)"', html).group(1)


def meter_tier(dash: dict) -> str:
    window = next(w for w in dash["windows"] if w["key"] == "five_hour")
    return "off" if window["withheld"] else window["state"]


def readout_tier(html: str) -> str:
    cls = readout_class(html)
    if "off" in cls:
        return "off"
    found = re.search(r"is-(\w+)", cls)
    return found.group(1) if found else "normal"


# -- one tier per state -----------------------------------------------------------


def test_a_healthy_readout_carries_no_colour_at_all() -> None:
    """89% left is the best news this tool has. It must not be dressed as caution."""
    html, dash = render(11.0)

    assert readout_class(html) == "readout"
    assert dash["burn"]["headroom"] == "89"
    assert "is-elevated" not in html and "is-critical" not in html


def test_elevated_and_critical_each_take_their_own_colour() -> None:
    elevated, _ = render(80.0)
    critical, _ = render(95.0)
    assert readout_class(elevated) == "readout is-elevated"
    assert readout_class(critical) == "readout is-critical"


def test_a_withheld_readout_stays_the_quiet_colour() -> None:
    html, dash = render(40.0, last_ok_offset=3600)  # collector stale
    assert readout_class(html) == "readout off"
    assert dash["burn"]["withheld"] is True


def test_the_api_severity_still_wins_over_the_threshold() -> None:
    """`magnitude_state` prefers the server's own word, and the readout inherits that."""
    html, _ = render(99.0, severity="normal")
    assert readout_class(html) == "readout"


# -- the hero and the meter describe one window -----------------------------------


CASES = {
    "healthy": {"pct": 11.0},
    "elevated by level": {"pct": 80.0},
    "critical by level": {"pct": 96.0},
    "server says normal at 99": {"pct": 99.0, "severity": "normal"},
    "server says critical at 10": {"pct": 10.0, "severity": "critical"},
    "steep climb, low level": {"pct": 20.0, "climb": 60.0},
    "no window open": {"pct": 0.0, "reset_in_s": 0},
    "stale collector": {"pct": 40.0, "last_ok_offset": 3600},
}


@pytest.mark.parametrize("case", list(CASES))
def test_the_readout_and_the_meter_never_disagree(case: str) -> None:
    """They are twelve pixels apart and describe the same window."""
    html, dash = render(**CASES[case])
    assert readout_tier(html) == meter_tier(dash), case


# -- a projection is not a level --------------------------------------------------


def test_a_steep_projection_does_not_colour_the_readout_on_its_own() -> None:
    """The rate says the window ends early; the level says there is plenty left.

    The old rule painted the readout red on the projection alone, so a healthy 20%
    used went red while the meter beneath it stayed quiet, and both were correct.
    The alert threshold is lifted out of the way here so the projection is the only
    thing firing: at its default the *rate* is elevated too, which is a separate,
    user-configured signal the meter has always honoured.
    """
    html, dash = render(20.0, climb=60.0, reset_in_s=3 * 3600, burn_alert=1000.0)

    assert dash["runway"]["exhaust_ts"] is not None, "the projection must actually fire"
    assert dash["burn"]["critical"] is True  # still computed, still reported
    assert "chip" not in _hero_heading(html)  # and the rate is not the thing firing
    assert readout_class(html) == "readout"  # so no colour at all
    assert dash["burn"]["why"].startswith("Exhausted at ")  # it says itself in words


def test_the_configured_rate_alert_still_reaches_both_together() -> None:
    """A rate over the threshold is a signal the user asked for, and both elements take it."""
    html, dash = render(20.0, climb=60.0, reset_in_s=3 * 3600, burn_alert=20.0)
    assert "elevated" in _hero_heading(html)
    assert readout_tier(html) == meter_tier(dash) == "elevated"


def _hero_heading(page: str) -> str:
    return page.split('<h2 class="lbl" id="br">')[1].split("</h2>")[0]


def test_the_hero_chip_follows_the_same_tier_as_the_colour() -> None:
    healthy, _ = render(11.0)
    elevated, _ = render(80.0)

    assert "chip" not in _hero_heading(healthy), "nothing to say, so nothing is said"
    assert "elevated" in _hero_heading(elevated)
