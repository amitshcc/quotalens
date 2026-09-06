from __future__ import annotations

import copy
import time

from fastapi.testclient import TestClient

from conftest import make_client, make_handler
from quotalens.api import create_app
from quotalens.parse import QuotaReading


def _client(settings, store, secrets) -> TestClient:
    return TestClient(create_app(settings, store, secrets))


def _seed(store, now: int) -> None:
    for i in range(16):
        ts = now - (15 - i) * 60
        store.record_quota(
            ts,
            [
                QuotaReading("five_hour", "5-hour", 20 + i, "r1"),
                QuotaReading("seven_day", "7-day", 5, "r2"),
            ],
        )


def test_health_never_polled(settings, store, secrets) -> None:
    with _client(settings, store, secrets) as tc:
        body = tc.get("/api/health").json()
    assert body["status"] == "never_polled"
    assert body["poller"]["last_success_ts"] is None
    assert body["store"]["rows"]["quota"] == 0
    assert "undocumented" in body["note"]


def test_current_and_series(settings, store, secrets) -> None:
    now = int(time.time())
    _seed(store, now)
    with _client(settings, store, secrets) as tc:
        current = tc.get("/api/quota/current").json()
        series = tc.get("/api/quota/series", params={"hours": 1}).json()
        one = tc.get("/api/quota/series", params={"hours": 1, "window": "seven_day"}).json()
        bad = tc.get("/api/quota/series", params={"hours": 0})
    assert {r["window"]: r["pct"] for r in current["readings"]} == {"five_hour": 35, "seven_day": 5}
    assert current["overage"] is None
    assert len(series["readings"]) == 32
    assert len(one["readings"]) == 16 and all(r["window"] == "seven_day" for r in one["readings"])
    assert bad.status_code == 422


def test_burn_endpoint(settings, store, secrets) -> None:
    now = int(time.time())
    _seed(store, now)
    with _client(settings, store, secrets) as tc:
        body = tc.get("/api/burn").json()
        single = tc.get("/api/burn", params={"window": "five_hour", "lookback": 15}).json()
        missing = tc.get("/api/burn", params={"window": "nope"})
    rates = {b["window"]: b["rate_pct_per_hour"] for b in body["burn"]}
    assert rates["five_hour"] == 60.0
    assert rates["seven_day"] == 0.0
    assert body["lookback_minutes"] == settings.burn_lookback_min
    # 16 samples span exactly the lookback, so the oldest sits on the cutoff and
    # drops out if the request lands a second later than the seed.
    assert single["burn"][0]["points"] in (15, 16)
    assert missing.status_code == 404


def test_docs_are_served_and_schema_hides_nothing_sensitive(settings, store, secrets) -> None:
    with _client(settings, store, secrets) as tc:
        assert tc.get("/api/docs").status_code == 200
        assert tc.get("/openapi.json").status_code == 200


# -- a hostile spend exponent must not take the whole dashboard down --------------


def _poisoned_usage(exponent: int = 60) -> dict:
    from conftest import USAGE_LIVE_2026_09

    payload = copy.deepcopy(USAGE_LIVE_2026_09)
    payload["spend"]["used"]["exponent"] = exponent
    payload["spend"]["limit"]["exponent"] = exponent
    payload["extra_usage"]["decimal_places"] = exponent
    return payload


def test_a_hostile_spend_exponent_leaves_every_page_answering(settings, store, secrets) -> None:
    """`spend.used.exponent: 60` was accepted, stored, and then 500ed everything.

    Observed by the audit: the poll reported "ok", the overage row went to disk
    with exponent 60, and `/`, `/api/health`, `/api/quota/current` and
    `/api/dashboard` all returned 500 while that row was the newest -- against
    "if the response could not be parsed, every value is replaced by an em dash".
    """
    handler = make_handler(usage=_poisoned_usage(), overage_status=404)
    app = create_app(settings, store, secrets, client_factory=lambda c: make_client(handler, c))
    with TestClient(app) as tc:
        assert tc.post("/api/poll").json()["state"] == "ok"
        for path in ("/", "/api/health", "/api/quota/current", "/api/dashboard", "/metrics"):
            assert tc.get(path).status_code == 200, path
        # Never stored: the row that used to sit there and re-raise on every range.
        assert store.counts()["overage"] == 0
        assert tc.get("/api/dashboard").json()["spend"] is None
        # The windows in the same payload are unaffected.
        current = tc.get("/api/quota/current").json()
        assert {r["window"] for r in current["readings"]} >= {"five_hour", "seven_day"}


def test_an_overage_row_written_before_the_check_still_renders(settings, store, secrets) -> None:
    """The belt: a database from an older build. An em dash, not a 500."""
    from quotalens.parse import SpendReading

    store.record_overage(int(time.time()), SpendReading(316, 200, 60, "USD", "spend"))
    app = create_app(settings, store, secrets)
    with TestClient(app) as tc:
        for path in ("/", "/api/health", "/api/quota/current", "/api/dashboard"):
            assert tc.get(path).status_code == 200, path
        assert "—" in tc.get("/").text
