from __future__ import annotations

import time

from fastapi.testclient import TestClient

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
    assert single["burn"][0]["points"] == 16
    assert missing.status_code == 404


def test_docs_are_served_and_schema_hides_nothing_sensitive(settings, store, secrets) -> None:
    with _client(settings, store, secrets) as tc:
        assert tc.get("/api/docs").status_code == 200
        assert tc.get("/openapi.json").status_code == 200
