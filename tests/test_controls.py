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
    assert '<option value="6h" selected>6h</option>' in html
    assert 'data-lookback="1h" aria-current="true"' in html
    assert 'data-refresh="off" aria-current="true"' in html
    assert "lookback 1h" in html  # the hero sentence states the lookback
    # the hidden series keeps its label (struck through) but draws no path
    assert 'class="el off">Weekly all</text>' in html
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
    assert re.search(r'<option value="\d+-\d+" selected>custom: ', html)


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


# -- Job C: forced poll is a real upstream request, with a visible cooldown --------


def test_force_poll_issues_upstream_request_and_reports_sample_ts(settings, store, secrets) -> None:
    from conftest import FakeRequest

    seen: list[FakeRequest] = []
    handler = make_handler(seen=seen)
    app = create_app(settings, store, secrets, client_factory=lambda c: make_client(handler, c))
    with TestClient(app) as tc:
        assert seen == []  # polling is disabled in tests: nothing until forced
        body = tc.post("/api/poll").json()
        assert [r.url.path for r in seen if r.url.path.endswith("/usage")]  # a real fetch happened
        assert body["accepted"] is True and body["sample_ts"] == body["last_success_ts"]
        assert body["sample_ts"] is not None and body["cooldown_s"] == 10
        page = tc.get("/").text
        assert re.search(r'id="poll" title="Force a poll now" data-cooldown="(9|10)"', page)
        assert "<button" in page and "disabled" not in page.split('id="poll"')[1].split(">")[0]
        again = tc.post("/api/poll").json()
        assert again["accepted"] is False and again["sample_ts"] is None
        assert len([r for r in seen if r.url.path.endswith("/usage")]) == 1  # suppressed: no fetch


def test_cooldown_counts_down_to_zero(settings, store, secrets) -> None:
    clock = {"t": 1_000_000.0}
    poller = Poller(
        settings,
        store,
        secrets,
        Redactor(),
        client_factory=lambda c: make_client(make_handler(), c),
        clock=lambda: clock["t"],
    )
    assert poller.cooldown_remaining() == 0
    asyncio.run(poller.force_poll())
    assert poller.cooldown_remaining() == 10
    clock["t"] += 4.5
    assert poller.cooldown_remaining() == 6
    clock["t"] += 5.5
    assert poller.cooldown_remaining() == 0
    assert asyncio.run(poller.force_poll())[0] is True


# -- Job D: range select ----------------------------------------------------------


def test_range_select_form_works_without_javascript(settings, store, secrets) -> None:
    app = _live_app(settings, store, secrets)
    now = int(time.time())
    with TestClient(app) as tc:
        html = tc.get("/?range=6h&hide=seven_day&lookback=1h").text
        custom = tc.get(f"/?range={now - 1200}-{now - 600}").text
        submitted = tc.get("/?range=24h&hide=seven_day&lookback=1h")  # what the form sends
    assert '<form method="get" action="/" class="ctl" id="range-form">' in html
    assert '<option value="6h" selected>6h</option>' in html
    assert '<option value="auto">auto</option>' in html
    assert '<input type="hidden" name="hide" value="seven_day">' in html
    assert '<input type="hidden" name="lookback" value="1h">' in html
    assert '<button type="submit" class="go">go</button>' in html
    assert re.search(r'<option value="\d+-\d+" selected>custom: ', custom)
    assert submitted.status_code == 200
    assert 'data-lookback="1h" aria-current="true"' in submitted.text
    assert 'label class="lbl" for="range"' in html  # keyboard: a labelled native select
