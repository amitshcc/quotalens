"""Job C: forced refresh rate limiting, URL-driven views, and no-JavaScript rendering."""

from __future__ import annotations

import asyncio
import re
import time
from importlib import resources

from fastapi.testclient import TestClient

from conftest import make_client, make_handler
from quotalens.api import create_app
from quotalens.parse import QuotaReading
from quotalens.poller import FORCE_MIN_INTERVAL_S, Poller
from quotalens.secrets import Redactor


def _seed(store, now: int, minutes: int = 30) -> None:
    for i in range(minutes):
        ts = now - (minutes - 1 - i) * 60
        store.record_quota(
            ts,
            [
                QuotaReading("five_hour", "5-hour", 20 + i, "r1", "normal", True),
                QuotaReading("seven_day", "7-day", 38, "r2", "normal", False),
                QuotaReading("limit:fable", "Fable", 69, "r3", "normal", False),
            ],
        )


def _live_app(settings, store, secrets):
    now = int(time.time())
    _seed(store, now)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    return app


# -- forced refresh -------------------------------------------------------------


def test_force_poll_is_rate_limited(settings, store, secrets) -> None:
    clock = {"t": 1_000_000.0}
    poller = Poller(
        settings,
        store,
        secrets,
        Redactor(),
        client_factory=lambda c: make_client(make_handler(), c),
        clock=lambda: clock["t"],
    )
    assert asyncio.run(poller.force_poll()) == (True, 0)
    assert poller.status.polls_ok == 1
    clock["t"] += 3
    accepted, retry_in = asyncio.run(poller.force_poll())
    assert not accepted and 6 <= retry_in <= 8
    assert "suppressed" in poller.status.extra["force_note"]["text"]
    assert poller.status.polls_ok == 1
    clock["t"] += FORCE_MIN_INTERVAL_S
    assert asyncio.run(poller.force_poll()) == (True, 0)
    assert "force_note" not in poller.status.extra
    assert poller.status.polls_ok == 2


def test_api_poll_endpoint_and_form_redirect(settings, store, secrets) -> None:
    app = create_app(
        settings, store, secrets, client_factory=lambda c: make_client(make_handler(), c)
    )
    with TestClient(app) as tc:
        first = tc.post("/api/poll").json()
        assert first["accepted"] is True and first["state"] == "ok"
        second = tc.post("/api/poll").json()
        assert second["accepted"] is False and second["retry_in_s"] > 0
        # The no-JS form returns to the same view and the suppression note is on the page.
        resp = tc.post("/poll?range=1h&lookback=5m", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/?range=1h&lookback=5m"
        page = tc.get("/?range=1h&lookback=5m").text
        assert "Forced poll suppressed" in page


# -- URL state and no-JS rendering ----------------------------------------------


def test_page_reflects_query_without_javascript(settings, store, secrets) -> None:
    app = _live_app(settings, store, secrets)
    with TestClient(app) as tc:
        html = tc.get("/?range=6h&lookback=1h&hide=seven_day&refresh=off").text
    # range and lookback controls are links with the active one marked
    assert re.search(
        r'<a class="rb on" href="[^"]*range=6h[^"]*" data-range="6h" aria-current="true"', html
    )
    assert 'data-lookback="1h" aria-current="true"' in html
    assert 'data-refresh="off" aria-current="true"' in html
    assert "lookback 1h" in html  # the hero sentence states the lookback
    # the hidden series keeps its label (struck through) but draws no path
    assert 'class="el off">7-day</text>' in html
    assert html.count('stroke="var(--s2)"') == 1  # only the brand mark
    # links carry the other state along; toggling seven_day back removes the hide param
    link = re.search(r'<a href="([^"]+)" class="el-link" data-series="seven_day"', html).group(1)
    assert "hide=" not in link and "range=6h" in link and "lookback=1h" in link
    action = "/poll?range=6h&amp;hide=seven_day&amp;lookback=1h&amp;refresh=off"
    assert f'<form method="post" action="{action}"' in html
    assert 'id="chart-data"' in html and '"key":"five_hour"' in html
    assert '"key":"seven_day"' not in html  # hidden series are not in the hover data


def test_invalid_query_falls_back_to_defaults(settings, store, secrets) -> None:
    app = _live_app(settings, store, secrets)
    with TestClient(app) as tc:
        html = tc.get("/?range=yesterday&hide=%3Cscript%3E&lookback=2d&refresh=now").text
        json_view = tc.get("/api/dashboard?range=yesterday").json()
    assert "<script>" not in html.replace('<script src="/static/', "").replace(
        '<script type="application/json"', ""
    )
    assert 'data-lookback="15m" aria-current="true"' in html
    assert json_view["range"]["auto"] is True and json_view["hidden"] == []


def test_custom_range_from_drag_is_bookmarkable(settings, store, secrets) -> None:
    app = _live_app(settings, store, secrets)
    now = int(time.time())
    with TestClient(app) as tc:
        view = tc.get(f"/api/dashboard?range={now - 1200}-{now - 600}").json()
        html = tc.get(f"/?range={now - 1200}-{now - 600}").text
    assert view["range"]["key"] == "custom"
    assert view["range"]["start"] == now - 1200 and view["range"]["end"] == now - 600
    assert 'id="custom-range"' in html


def test_controls_are_keyboard_operable_links_with_focus_style() -> None:
    css = resources.files("quotalens.web").joinpath("app.css").read_text()
    assert ":focus-visible{outline:var(--focus-w) solid var(--focus)" in css
    assert ".el-link:focus-visible" in css


def test_scripts_stay_within_budget_and_have_no_external_requests() -> None:
    total = 0
    for name in ("app.js", "chart.js"):
        text = resources.files("quotalens.web").joinpath(name).read_text()
        total += len(text.splitlines())
        assert "http://" not in text and "https://" not in text
    assert total < 600
