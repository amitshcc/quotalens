"""A window whose reset time has passed is not a current reading of anything.

The reported failure: at 14:22 the hero said "no window" while the meter twelve
pixels below said "Session 40% used, resets 14:00, +35 pts since the last reset".
Three contradictions of one fact — a headroom that reads as a live budget, a past
time worded as pending, and a delta belonging to a window that had closed — plus
the ring and the favicon repeating the phantom 40%.

The cause was `compute_runway` detecting the lapse, refusing to project, and then
passing the stored percentage through anyway. These tests hold the invariant that
replaced it: never present a session percentage as current once its window's reset
time has passed, and never let the hero and the meter disagree about whether one
is open.
"""

from __future__ import annotations

import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import quotalens.api as api_module
from quotalens.api import create_app
from quotalens.config import settings_from_env
from quotalens.dashboard import EM_DASH
from quotalens.parse import QuotaReading, parse_usage
from quotalens.runway import compute_runway
from quotalens.secrets import MemorySecretStore
from quotalens.state import STALE_AFTER_INTERVALS
from quotalens.store import Store

NOW = 1_800_000_000
INTERVAL = 60


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def render(rows: list[tuple[int, list[QuotaReading]]], now: int = NOW, last_ok: int | None = None):
    """Render the whole page at a fixed instant, and hand back the HTML and the JSON."""
    settings = settings_from_env().with_overrides(
        db_path=Path(tempfile.mkdtemp()) / "t.db", poll_interval_s=INTERVAL
    )
    store = Store(settings.db_path)
    for ts, readings in rows:
        store.record_quota(ts, readings)
    app = create_app(settings, store, MemorySecretStore("cookie"))
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now if last_ok is None else last_ok
    real = api_module.time.time
    api_module.time.time = lambda: now
    try:
        with TestClient(app) as client:
            return (
                client.get("/").text,
                client.get("/api/dashboard").json(),
                client.get("/api/quota/current").json(),
                client.get("/favicon.svg").text,
                client.get("/metrics").text,
            )
    finally:
        api_module.time.time = real
        store.close()


def session_rows(pct: float, reset_ts: int | None, count: int = 60, until: int = NOW):
    """A climbing session series whose newest sample is at ``until``."""
    reset = iso(reset_ts) if reset_ts is not None else None
    return [
        (
            until - (count - 1 - i) * 60,
            [QuotaReading("five_hour", "5-hour", pct, reset, None, True)],
        )
        for i in range(count)
    ]


def meter_footer(html: str) -> tuple[str, str]:
    m = re.search(r'<div class="foot m"><span>([^<]*)</span><span>([^<]*)</span>', html)
    return m.groups() if m else ("", "")


def hero_window_text(html: str) -> str:
    m = re.search(r'<div class="resets"><span class="lbl">resets in</span>(.*?)</div>', html, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""


# -- the derivation ---------------------------------------------------------------


def test_a_lapsed_window_yields_no_live_percentage() -> None:
    """The branch used to detect the lapse and then pass `pct` straight through."""
    lapsed = compute_runway(40.0, 0.0, 900, NOW - 22 * 60, NOW)
    assert lapsed.remaining_s == 0
    assert lapsed.pct is None and lapsed.headroom_pct is None
    assert lapsed.exhaust_ts is None and lapsed.sustainable is None
    assert lapsed.verdict.startswith("The session window ended at")


def test_an_undated_reading_is_current_because_the_server_just_sent_it() -> None:
    """`resets_at: null` means no window is running, not that the value is unknown.

    This is what the endpoint actually returns between windows, and discarding it
    was what left the previous window's number on the page.
    """
    open_none = compute_runway(0.0, 0.0, 900, None, NOW)
    assert open_none.pct == 0.0 and open_none.headroom_pct == 100.0
    assert open_none.verdict.startswith("No session running")


def test_the_parser_keeps_an_undated_session_value_and_records_why() -> None:
    parsed = parse_usage({"five_hour": {"utilization": 0.0, "resets_at": None}})
    reading = next(r for r in parsed.readings if r.window == "five_hour")
    assert reading.pct == 0.0 and reading.resets_at is None
    assert ("five_hour", "no resets_at, value kept") in [(b.key, b.reason) for b in parsed.ignored]


def test_an_undated_weekly_block_is_still_only_a_diagnostic() -> None:
    """Narrow on purpose: an undated weekly block must not invent a meter."""
    parsed = parse_usage(
        {
            "five_hour": {"utilization": 3.0, "resets_at": iso(NOW + 3600)},
            "seven_day_sonnet": {"utilization": 0, "resets_at": None},
        }
    )
    assert "seven_day_sonnet" not in {r.window for r in parsed.readings}


# -- the hero and the meter cannot disagree ---------------------------------------


# (rows, last_success_ts, is a window open, meter footer, percentage the meter shows)
CASES = {
    "live window": (session_rows(40.0, NOW + 90 * 60), NOW, True, "resets 15:00", 40.0),
    "lapsed window": (session_rows(40.0, NOW - 22 * 60), NOW, False, "ended 13:08", None),
    "no window open": (session_rows(0.0, None), NOW, False, "no window open", 0.0),
    "stale collector": (
        session_rows(40.0, NOW + 90 * 60),
        NOW - 3600,
        False,
        "last ok 13:30",
        None,
    ),
}


@pytest.mark.parametrize("case", list(CASES))
def test_the_hero_and_the_meter_agree_about_whether_a_window_is_open(case: str) -> None:
    """The hero and the meter are twelve pixels apart and must not contradict.

    "resets in: no window" beside "resets 14:00" was one bug wearing two labels,
    so this pins both across every state the view can be in. Note that "no window
    open" still shows a percentage: an undated reading is the current value of a
    window that is not running, which is the 0% the endpoint reports between them.
    """
    rows, last_ok, expect_open, expect_footer, expect_pct = CASES[case]
    html, dash, _current, _icon, _metrics = render(rows, last_ok=last_ok)
    hero, footer = hero_window_text(html), meter_footer(html)[0]

    hero_open = "no window" not in hero and set(hero) != {EM_DASH}
    meter_open = footer.startswith("resets ") and "(passed)" not in footer
    assert hero_open == meter_open, f"{case}: hero {hero!r} vs meter {footer!r}"
    assert hero_open is expect_open
    assert footer == expect_footer
    assert dash["windows"][0]["pct"] == expect_pct
    assert dash["windows"][0]["withheld"] is (expect_pct is None)


# -- per-window freshness ---------------------------------------------------------


def test_a_window_that_stops_appearing_is_withheld_even_while_the_collector_is_healthy() -> None:
    """One block inside a healthy payload can go dark; the collector cannot see it.

    Staleness was tracked per collector only, so `last_success_ts` stayed current
    and the meter kept rendering the last stored row at full confidence.
    """
    weekly = QuotaReading("seven_day", "7-day", 96.0, iso(NOW + 3 * 86400), None, False)
    stopped = NOW - (STALE_AFTER_INTERVALS * INTERVAL + 60)
    rows = [
        (stopped, [QuotaReading("five_hour", "5-hour", 40.0, iso(NOW + 3600), None, True), weekly]),
        *[(NOW - i * 60, [weekly]) for i in range(5, 0, -1)],  # only the weekly keeps arriving
    ]
    html, dash, current, _icon, metrics = render(rows)

    session = next(w for w in dash["windows"] if w["key"] == "five_hour")
    weekly_view = next(w for w in dash["windows"] if w["key"] == "seven_day")
    assert session["withheld"] is True and session["pct"] is None
    assert weekly_view["withheld"] is False and weekly_view["pct"] == 96.0
    assert dash["collector"]["kind"] == "ok", "the collector itself is healthy"
    assert ">40<" not in html and "40 %" not in html

    stale_reading = next(r for r in current["readings"] if r["window"] == "five_hour")
    assert stale_reading["pct"] is None and stale_reading["last_pct"] == 40.0
    assert stale_reading["stale"] is True
    assert 'quotalens_quota_percent{label="5-hour",window="five_hour"} NaN' in metrics


# -- the reported state, end to end -----------------------------------------------


def test_the_reported_state_shows_no_phantom_percentage_anywhere() -> None:
    """08:59-13:59 seen at 14:22: no 40%, no "resets 14:00", in HTML, ring or favicon."""
    ended = NOW - 22 * 60
    html, dash, current, icon, metrics = render(session_rows(40.0, ended, until=ended))

    # Everything outside the chart's own data island, which legitimately carries the
    # history that produced the window that has now closed.
    visible = html.split('id="chart-data"')[0] + html.split("</script>")[-1]
    for forbidden in ("40 %", ">40<", "40.0", "resets 14"):
        assert forbidden not in visible, forbidden
    assert hero_window_text(html) == "no window"
    assert meter_footer(html) == (f"ended {time.strftime('%H:%M', time.localtime(ended))}", "")
    assert dash["runway"]["headroom_pct"] is None
    assert dash["burn"]["why"].startswith("The session window ended at")

    # The ring and the favicon read the same view, so they inherit the fix.
    assert "rotate(-90" not in icon, "the favicon drew an arc for a closed window"
    ring = re.search(r'<span class="brand">(<svg.*?</svg>)', html, re.S).group(1)
    assert "rotate(-90" not in ring, "the header ring drew an arc for a closed window"

    # The chart shades the lapse rather than running the trace flat to the edge.
    assert 'class="idle"' in html and dash["idle_minutes"] >= 22
    assert current["readings"][0]["pct"] is None
    assert 'quotalens_quota_percent{label="5-hour",window="five_hour"} NaN' in metrics
