"""The page is shaped by the readings: a Pro payload shows no Fable, a Max one does.

The Pro fixture is **assumed from the help centre, not observed** (2026-10-01):
see ``tests/fixtures/README.md``.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from datetime import UTC, datetime
from http.server import HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "qa"))

import fake_claude  # noqa: E402

from quotalens import plan  # noqa: E402

FIXTURE_NOW = datetime(2026, 10, 1, 12, tzinfo=UTC).timestamp()
FIXTURE_ARGS = {
    "now": FIXTURE_NOW,
    "session_pct": 20.5,
    "session_end": FIXTURE_NOW + 3 * 3600,
    "weekly_pct": 40.1,
    "weekly_end": datetime(2026, 10, 5, 1, tzinfo=UTC).timestamp(),
    "grant_end": datetime(2026, 11, 4, tzinfo=UTC).timestamp(),
}
FIXTURE_FILES = {"max": "usage_max.json", "pro": "usage_pro_assumed.json"}


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / FIXTURE_FILES[name]).read_text())


# -- the fixtures and the fake ---------------------------------------------------------


@pytest.mark.parametrize("name", ["max", "pro"])
def test_fixture_is_what_the_fake_sends(name: str) -> None:
    assert load_fixture(name) == fake_claude.build_usage(name, **FIXTURE_ARGS)


def test_pro_shape_has_no_fable_limit_and_a_100_dollar_credit() -> None:
    pro, max_ = load_fixture("pro"), load_fixture("max")
    assert [entry["kind"] for entry in pro["limits"]] == ["session", "weekly_all"]
    assert [entry["kind"] for entry in max_["limits"]] == ["session", "weekly_all", "weekly_scoped"]
    assert max_["limits"][2]["scope"]["model"]["display_name"] == "Fable"
    assert pro["iguana_necktie"]["limit_dollars"] == 100
    assert max_["iguana_necktie"]["limit_dollars"] == 250
    assert "seven_day_breakdown" in pro and "seven_day_breakdown" in max_
    assert "fable" not in json.dumps(pro).lower()


@pytest.fixture
def fake_server():
    fake_claude.STATE.update(mode="ok", plan="max")
    server = HTTPServer(("127.0.0.1", 0), fake_claude.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    fake_claude.STATE.update(mode="ok", plan="max")


def _get(url: str, cookie: str = "") -> dict:
    request = urllib.request.Request(url, headers={"Cookie": cookie})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def _post(url: str) -> dict:
    request = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def test_fake_switches_plan_and_its_bootstrap_follows(fake_server: str) -> None:
    cookie = "sessionKey=x; lastActiveOrg=org-aaaa-bbbb-cccc"
    usage = _get(fake_server + "/api/organizations/org-aaaa-bbbb-cccc/usage")
    assert len(usage["limits"]) == 3
    boot = _get(fake_server + "/api/bootstrap", cookie)
    assert plan.from_bootstrap(boot, "org-aaaa-bbbb-cccc").label == "Max 20x"

    assert _post(fake_server + "/mode/plan-pro") == {"mode": "ok"}
    usage = _get(fake_server + "/api/organizations/org-aaaa-bbbb-cccc/usage")
    assert [entry["kind"] for entry in usage["limits"]] == ["session", "weekly_all"]
    assert usage["iguana_necktie"]["limit_dollars"] == 100
    boot = _get(fake_server + "/api/bootstrap", cookie)
    assert plan.from_bootstrap(boot, "org-aaaa-bbbb-cccc").label == "Pro"

    _post(fake_server + "/mode/plan-max")
    assert len(_get(fake_server + "/api/organizations/o/usage")["limits"]) == 3


# -- the whole page, from each fixture -------------------------------------------------
#
# The real poller runs over ten days of history drawn by the same `build_usage` that
# wrote the fixtures (so Weeks has a closed week and the budget has session windows),
# then takes one last poll that is the fixture file verbatim, at the fixture's clock.
# `time.time` is frozen at each step, so the page is rendered "at" the fixture.

HISTORY_START = datetime(2026, 9, 21, 2, tzinfo=UTC).timestamp()  # just after a Monday reset
HISTORY_STEP_S = 20 * 60
SESSION_S = 5 * 3600
WEEK_S = 7 * 86400
FIRST_RESET = datetime(2026, 9, 21, 1, tzinfo=UTC).timestamp()
SESSION_PTS_PER_H = 5.0  # a quarter of a session per window
WEEKLY_PER_SESSION_PT = 0.1  # ten points of the week per full session
WEEKLY_CAP_BEFORE_FIXTURE = 40.0  # never above the fixture's 40.1: a fall would read as a boost


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _history_payload(name: str, t: float, weekly: float) -> dict:
    final_end = FIXTURE_ARGS["session_end"]
    windows_back = int((final_end - t) // SESSION_S)
    session_end = final_end - windows_back * SESSION_S
    into = SESSION_S - (session_end - t)
    weekly_end = FIRST_RESET + WEEK_S * (int((t - FIRST_RESET) // WEEK_S) + 1)
    session_pct = round(SESSION_PTS_PER_H * into / 3600, 1)
    return fake_claude.build_usage(
        name,
        t,
        session_pct,
        session_end,
        round(weekly, 1),
        weekly_end,
        FIXTURE_ARGS["grant_end"],
    )


def _bootstrap_for(name: str) -> dict:
    from conftest import ORG

    return fake_claude.build_bootstrap(name, ORG)


def _render(name, monkeypatch, settings, store, secrets) -> tuple[str, dict]:
    """``(html, api)``: the page and every JSON route, rendered from one fixture."""
    import asyncio
    import time

    from fastapi.testclient import TestClient

    from conftest import json_response, make_client
    from quotalens.api import create_app
    from quotalens.poller import Poller
    from quotalens.secrets import Redactor

    clock = Clock(HISTORY_START)
    monkeypatch.setattr(time, "time", clock)
    current: dict = {}

    def handler(req):
        if req.url.path == "/api/bootstrap":
            return json_response(200, _bootstrap_for(name))
        if req.url.path.endswith("/usage"):
            return json_response(200, current["usage"])
        return json_response(404, {"error": "not found"})

    poller = Poller(
        settings,
        store,
        secrets,
        Redactor(),
        client_factory=lambda cookie: make_client(handler, cookie),
        clock=clock,
    )

    async def drive() -> None:
        weekly, last_session, last_week_end = 0.0, 0.0, None
        t = HISTORY_START
        while t < FIXTURE_NOW:
            clock.now = t
            payload = _history_payload(name, t, weekly)
            week_end = payload["seven_day"]["resets_at"]
            if last_week_end is not None and week_end != last_week_end:
                weekly = 0.0
            session = payload["five_hour"]["utilization"]
            weekly = min(
                WEEKLY_CAP_BEFORE_FIXTURE,
                weekly + max(0.0, session - last_session) * WEEKLY_PER_SESSION_PT,
            )
            last_session, last_week_end = session, week_end
            current["usage"] = _history_payload(name, t, weekly)
            await poller.poll_once()
            t += HISTORY_STEP_S
        clock.now = FIXTURE_NOW
        current["usage"] = load_fixture(name)
        await poller.poll_once()
        await poller.stop()

    asyncio.run(drive())
    assert poller.status.state == "ok"

    app = create_app(settings, store, secrets)
    app.state.qw.poller.status = poller.status
    routes = (
        "health",
        "quota/current",
        "budget",
        "weeks",
        "credits",
        "breakdown",
        "pace",
        "heatmap",
        "burn",
        "events?kind=subcap_violation",
    )
    with TestClient(app) as tc:
        html = tc.get("/").text
        api = {route: tc.get(f"/api/{route}").json() for route in routes}
    return html, api


@pytest.fixture
def pro_page(monkeypatch, settings, store, secrets) -> tuple[str, dict]:
    return _render("pro", monkeypatch, settings, store, secrets)


@pytest.fixture
def max_page(monkeypatch, settings, store, secrets) -> tuple[str, dict]:
    return _render("max", monkeypatch, settings, store, secrets)


def _chips(html: str) -> list[str]:
    import re

    group = re.search(r'<span class="ctl series".*?</span>(.*?)</span>', html, re.S)
    return re.findall(r"<a [^>]*>([^<]*)</a>", group.group(1)) if group else []


def test_pro_page_shows_only_what_pro_has(pro_page) -> None:
    import re

    html, api = pro_page
    assert "QuotaLens · Pro<" in html  # the plan reached the page, and changed nothing else
    assert "fable" not in html.lower()
    assert "half of weekly pool" not in html
    assert _chips(html) == ["All", "Session", "Weekly all"]
    # The budget: exactly one weekly limit, all models.
    assert [b["label"] for b in api["budget"]["budgets"]] == ["Weekly — all models"]
    # Readings: no Fable window, and nothing reads as "Fable 0%" or "Fable —".
    assert {r["window"] for r in api["quota/current"]["readings"]} == {"five_hour", "seven_day"}
    # Weeks: rows exist, and every Fable cost is absent rather than zero.
    weeks_rows = api["weeks"]["weeks"]
    assert weeks_rows, "the history must close at least one week"
    for row in weeks_rows:
        assert row["fable_cost"] is None and row["fable_low"] is None
        assert row["fable_high"] is None and not row["fable_n"]
    table = html.split('<section class="screen weeks">', 1)[1].split("</table>", 1)[0]
    assert table.count("<tr") >= 2  # a header and at least one week
    assert "<br>" not in table  # the Fable line is the cell's second line; there is none
    # The subcap check had nothing to compare and recorded nothing.
    assert api["events?kind=subcap_violation"]["events"] == []
    # The credit grant is Pro's $100.
    assert re.search(r"of \$100\b", html) and "of $250" not in html
    for route, body in api.items():
        dumped = json.dumps(body).lower()
        for key in ("fable_cost", "fable_low", "fable_high", "fable_n"):
            dumped = dumped.replace(f'"{key}"', '"_"')
        assert "fable" not in dumped, route


def test_max_page_shows_the_sub_capped_meter(max_page) -> None:
    html, api = max_page
    assert "QuotaLens · Max 20x<" in html
    assert ">Weekly — Fable " in html  # the meter
    assert "half of weekly pool" in html  # the note
    assert _chips(html) == ["All", "Session", "Weekly all", "Weekly Fable"]
    labels = [b["label"] for b in api["budget"]["budgets"]]
    assert labels[0] == "Weekly — all models" and len(labels) == 2 and "Fable" in labels[1]
    assert "limit:fable" in {r["window"] for r in api["quota/current"]["readings"]}
    assert "of $250" in html
    # The same checks that find nothing on Pro find something here, so their silence
    # there is absence, not a check that never looks.
    table = html.split('<section class="screen weeks">', 1)[1].split("</table>", 1)[0]
    assert "<br>" in table and "Fable" in table  # the Fable line in "one full session"
    assert api["events?kind=subcap_violation"]["events"]  # Fable 66 against a young week
