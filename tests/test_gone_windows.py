"""A window that stops arriving leaves the current views by itself (WP-35).

Gone = absent from the last 3 good polls *and* its newest reading older than 15 minutes
(both measured against the newest poll). ``five_hour`` is exempt. A gone window keeps
its history on the chart; it has no meter, chip, budget row, Weeks column or subcap.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

from conftest import json_response, make_client
from quotalens.dashboard import (
    GONE_AFTER_S,
    WINDOW_BACK_KIND,
    WINDOW_GONE_KIND,
    current_quota,
    current_windows,
)
from quotalens.parse import QuotaReading
from quotalens.poller import Poller
from quotalens.secrets import Redactor
from quotalens.store import QuotaRow

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "qa"))

import fake_claude

T0 = 1_790_000_000
STEP = 5 * 60


def _row(ts: int, window: str) -> QuotaRow:
    return QuotaRow(ts, window, window, 10.0, "2026-10-05T01:00:00+00:00")


# -- the rule ----------------------------------------------------------------------------


def test_gone_after_three_absent_polls_and_fifteen_minutes() -> None:
    polls = [T0 + 20 * 60, T0 + 15 * 60, T0 + 10 * 60]  # Fable's last reading was at T0
    latest = [_row(polls[0], "seven_day"), _row(T0, "limit:fable")]
    assert [r.window for r in current_windows(latest, polls)] == ["seven_day"]


def test_not_gone_after_two_absent_polls() -> None:
    # Older than 15 minutes, but the third-newest poll still carried it.
    polls = [T0 + 40 * 60, T0 + 30 * 60, T0]
    latest = [_row(polls[0], "seven_day"), _row(T0, "limit:fable")]
    assert {r.window for r in current_windows(latest, polls)} == {"seven_day", "limit:fable"}


def test_not_gone_while_younger_than_fifteen_minutes() -> None:
    polls = [T0 + GONE_AFTER_S, T0 + 600, T0 + 300]  # three absent polls, 15 min exactly
    latest = [_row(polls[0], "seven_day"), _row(T0, "limit:fable")]
    assert {r.window for r in current_windows(latest, polls)} == {"seven_day", "limit:fable"}


def test_not_gone_with_fewer_than_three_polls() -> None:
    latest = [_row(T0 + 3600, "seven_day"), _row(T0, "limit:fable")]
    assert len(current_windows(latest, [T0 + 3600, T0 + 1800])) == 2


def test_five_hour_is_exempt() -> None:
    polls = [T0 + 3 * 3600, T0 + 2 * 3600, T0 + 3600]
    latest = [_row(polls[0], "seven_day"), _row(T0, "five_hour")]
    assert {r.window for r in current_windows(latest, polls)} == {"seven_day", "five_hour"}


def test_store_counts_polls_by_distinct_reading_time(store) -> None:
    fable = QuotaReading("limit:fable", "Fable", 50.0, "2026-10-05T01:00:00+00:00")
    weekly = QuotaReading("seven_day", "7-day", 40.0, "2026-10-05T01:00:00+00:00")
    store.record_quota(T0, [weekly, fable])
    for k in range(1, 4):
        store.record_quota(T0 + k * 600, [weekly])
    assert store.recent_poll_ts(3) == [T0 + 1800, T0 + 1200, T0 + 600]
    assert [r.window for r in current_quota(store)] == ["seven_day"]
    assert {r.window for r in store.latest_quota()} == {"seven_day", "limit:fable"}  # history


# -- Max -> Pro on the fake, through the real poller and the page ------------------------


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def account(monkeypatch, settings, store, secrets):
    """A poller against ``qa/fake_claude.py`` on a frozen clock, and its mode switch."""
    clock = Clock(T0)
    monkeypatch.setattr(time, "time", clock)  # the fake reads the same clock
    fake_claude.STATE.update(
        mode="ok",
        plan="max",
        pct=10.0,
        weekly=30.0,
        session_end=T0 + 4.5 * 3600,
        weekly_end=T0 + 3 * 86400,
        grant_end=T0 + 30 * 86400,
    )
    server = HTTPServer(("127.0.0.1", 0), fake_claude.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def forward(req):
        request = urllib.request.Request(base + req.url.path, headers=req.headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json_response(response.status, json.loads(response.read()))
        except urllib.error.HTTPError as err:  # the fake's 404 for the overage endpoint
            return json_response(err.code, json.loads(err.read() or b"{}"))

    poller = Poller(
        settings,
        store,
        secrets,
        Redactor(),
        client_factory=lambda cookie: make_client(forward, cookie),
        clock=clock,
    )

    def mode(name: str) -> None:
        post = urllib.request.Request(f"{base}/mode/{name}", data=b"", method="POST")
        urllib.request.urlopen(post, timeout=5).close()

    def polls(count: int) -> None:
        async def drive() -> None:
            for _ in range(count):
                clock.now += STEP
                await poller.poll_once()
                assert poller.status.state == "ok", poller.status.last_error

        asyncio.run(drive())

    yield clock, poller, mode, polls
    server.shutdown()
    server.server_close()
    fake_claude.STATE.update(mode="ok", plan="max")


def _page(settings, store, secrets, poller, query: str = "") -> tuple[str, dict]:
    from fastapi.testclient import TestClient

    from quotalens.api import create_app

    app = create_app(settings, store, secrets)
    app.state.qw.poller.status = poller.status
    with TestClient(app) as tc:
        html = tc.get("/" + query).text
        api = {r: tc.get(f"/api/{r}").json() for r in ("quota/current", "budget", "weeks")}
    return html, api


def _chips(html: str) -> list[str]:
    group = re.search(r'<span class="ctl series".*?</span>(.*?)</span>', html, re.S)
    return re.findall(r"<a [^>]*>([^<]*)</a>", group.group(1)) if group else []


def _chart_keys(html: str) -> dict[str, str]:
    raw = html.split('id="chart-data">', 1)[1].split("</script>", 1)[0]
    data = json.loads(raw)
    series = data["series"] if isinstance(data, dict) else data
    return {s["key"]: s["label"] for s in series}


def _without_events(html: str) -> str:
    """The page minus the sidebar's event lines, which record that Fable went."""
    return re.sub(r'<p class="ev[^"]*">.*?</p>', "", html, flags=re.S)


def test_max_to_pro_switch_leaves_no_trace_of_the_gone_meter(
    account, settings, store, secrets
) -> None:
    _clock, poller, mode, polls = account
    mode("plan-max")
    polls(6)  # 30 min on Max: a Fable meter
    html, _ = _page(settings, store, secrets, poller)
    assert "Weekly Fable" in _chips(html)

    mode("plan-pro")
    polls(3)  # three polls without it, but its last reading is only 15 min old
    assert "limit:fable" in {r.window for r in current_quota(store)}
    assert not store.recent_events(kind=WINDOW_GONE_KIND)
    polls(33)  # 3 h on Pro in all

    gone = store.recent_events(kind=WINDOW_GONE_KIND)
    assert len(gone) == 1 and gone[0].detail.startswith("limit:fable: ")
    html, api = _page(settings, store, secrets, poller, "?range=1h")
    assert "fable" not in _without_events(html).lower()
    assert _chips(html) == ["All", "Session", "Weekly all"]
    assert {r["window"] for r in api["quota/current"]["readings"]} == {"five_hour", "seven_day"}
    assert [b["label"] for b in api["budget"]["budgets"]] == ["Weekly — all models"]
    assert all(row["fable_cost"] is None for row in api["weeks"]["weeks"])


def test_gone_window_keeps_its_history_on_the_chart(account, settings, store, secrets) -> None:
    _clock, poller, mode, polls = account
    polls(6)
    mode("plan-pro")
    polls(12)  # 1 h on Pro: Fable gone, its 30 min of history inside a 6 h range
    html, _ = _page(settings, store, secrets, poller, "?range=6h")
    keys = _chart_keys(html)
    assert "limit:fable" in keys and "Fable" in keys["limit:fable"]  # drawn, with its label
    assert "Weekly Fable" not in _chips(html)  # but no chip
    assert ">Weekly — Fable " not in html  # and no meter


def test_window_back_when_it_returns(account, settings, store, secrets) -> None:
    _clock, poller, mode, polls = account
    polls(4)
    mode("plan-pro")
    polls(6)
    mode("plan-max")
    polls(1)

    assert [e.kind for e in store.recent_events(limit=50) if "window_" in e.kind] == [
        WINDOW_BACK_KIND,
        WINDOW_GONE_KIND,
    ]
    assert "limit:fable" in {r.window for r in current_quota(store)}
    polls(3)  # still back: no second event
    assert len(store.recent_events(kind=WINDOW_BACK_KIND)) == 1
    html, _ = _page(settings, store, secrets, poller)
    assert "Weekly Fable" in _chips(html)
