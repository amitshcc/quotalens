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


def test_credits_endpoint_lists_grants_in_dollars(settings, store, secrets) -> None:
    from datetime import UTC, datetime, timedelta

    from quotalens.parse import CreditGrant

    now = int(time.time())
    ends = (datetime.fromtimestamp(now, UTC) + timedelta(days=36)).isoformat()
    old = (datetime.fromtimestamp(now, UTC) - timedelta(days=9)).isoformat()
    store.record_grants(
        now,
        [
            CreditGrant("iguana_necktie", "Cloud session credit", 2129, 25000, 22871, 8.5, ends),
            CreditGrant("gone", "gone (unrecognised)", 0, 10000, 10000, 0.0, old),
        ],
    )
    with _client(settings, store, secrets) as tc:
        body = tc.get("/api/credits").json()
    assert body == {
        "grants": [
            {
                "key": "iguana_necktie",
                "label": "Cloud session credit",
                "used": 21.29,
                "limit": 250.0,
                "remaining": 228.71,
                "pct": 8.52,
                "expires_at": ends,
                "locked_reason": None,
            }
        ]
    }


def test_credits_endpoint_is_empty_without_grants(settings, store, secrets) -> None:
    with _client(settings, store, secrets) as tc:
        assert tc.get("/api/credits").json() == {"grants": []}


def test_breakdown_endpoint(settings, store, secrets) -> None:
    from datetime import UTC, datetime

    from quotalens.budget import week_key
    from quotalens.parse import QuotaReading, SurfaceBreakdown, SurfaceShare

    now = int(time.time())
    with _client(settings, store, secrets) as tc:
        assert tc.get("/api/breakdown").json()["breakdown"] is None
    started = datetime.fromisoformat(week_key(now)).replace(hour=1, tzinfo=UTC).isoformat()
    store.record_quota(now, [QuotaReading("seven_day", "7-day", 50, "r", "normal", False)])
    rows = [SurfaceShare("cowork", "Cowork", 76.0), SurfaceShare("chat", "Chats", 24.0)]
    store.record_breakdown(now, SurfaceBreakdown(None, started, rows))
    with _client(settings, store, secrets) as tc:
        body = tc.get("/api/breakdown").json()["breakdown"]
    assert body["window_started_at"] == started and body["is_current_week"] is True
    assert [(r["key"], r["percent"], r["of_limit"]) for r in body["rows"]] == [
        ("cowork", 76.0, 38),
        ("chat", 24.0, 12),
    ]


def test_events_after_id_exclusive_ascending(settings, store, secrets) -> None:
    for ts, kind in [(100, "a"), (200, "b"), (200, "a"), (300, "a")]:
        store.record_event(kind, f"d{ts}", ts=ts)
    with _client(settings, store, secrets) as tc:
        body = tc.get("/api/events", params={"after_id": 1}).json()
        paged = tc.get("/api/events", params={"after_id": 1, "limit": 1}).json()
        kinded = tc.get("/api/events", params={"after_id": 0, "kind": "b"}).json()
        empty = tc.get("/api/events", params={"after_id": 4}).json()
        bad = tc.get("/api/events", params={"after_id": -1})
    assert [e["id"] for e in body["events"]] == [2, 3, 4]
    assert [(e["ts"], e["kind"]) for e in body["events"]] == [(200, "b"), (200, "a"), (300, "a")]
    assert body["next_after_id"] == 4
    assert len(paged["events"]) == 1 and paged["next_after_id"] == 2
    assert [e["kind"] for e in kinded["events"]] == ["b"] and kinded["next_after_id"] == 2
    assert empty["events"] == [] and empty["next_after_id"] == 4
    assert bad.status_code == 422
    assert "next_since" not in body


def test_events_back_dated_week_reset_reaches_a_follower_past_its_ts(
    settings, store, secrets
) -> None:
    """A week_reset is written with ts = the week's close, after later events exist."""
    store.record_event("poll_error", "timeout", ts=1_000)
    store.record_event("poll_error", "timeout", ts=2_000)
    with _client(settings, store, secrets) as tc:
        first = tc.get("/api/events", params={"after_id": 0}).json()
        cursor = first["next_after_id"]
        store.record_event("week_reset", "{}", ts=900)  # back-dated behind both
        second = tc.get("/api/events", params={"after_id": cursor}).json()
    assert [e["ts"] for e in first["events"]] == [1_000, 2_000]
    assert [(e["kind"], e["ts"]) for e in second["events"]] == [("week_reset", 900)]


def test_events_sharing_a_ts_across_a_page_boundary_are_both_delivered(
    settings, store, secrets
) -> None:
    store.record_event("week_reset", "seven_day", ts=5_000)
    store.record_event("week_reset", "limit:fable", ts=5_000)
    seen = []
    with _client(settings, store, secrets) as tc:
        cursor = 0
        for _ in range(3):
            page = tc.get("/api/events", params={"after_id": cursor, "limit": 1}).json()
            seen += [e["detail"] for e in page["events"]]
            cursor = page["next_after_id"]
    assert seen == ["seven_day", "limit:fable"]


def test_events_without_a_cursor_newest_first_with_ids(settings, store, secrets) -> None:
    for ts in (100, 200, 300):
        store.record_event("a", "d", ts=ts)
    with _client(settings, store, secrets) as tc:
        body = tc.get("/api/events").json()
    assert [(e["id"], e["ts"]) for e in body["events"]] == [(3, 300), (2, 200), (1, 100)]
    assert "next_after_id" not in body and "next_since" not in body


def test_health_has_profile(settings, store, secrets) -> None:
    with _client(settings, store, secrets) as tc:
        assert tc.get("/api/health").json()["profile"] == "default"
        named = _client(settings.with_overrides(profile="work"), store, secrets)
        with named as tc2:
            assert tc2.get("/api/health").json()["profile"] == "work"


def test_health_version_matches_package(settings, store, secrets) -> None:
    import quotalens

    with _client(settings, store, secrets) as tc:
        assert tc.get("/api/health").json()["version"] == quotalens.__version__ == "2.0.0"
