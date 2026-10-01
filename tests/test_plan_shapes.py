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
