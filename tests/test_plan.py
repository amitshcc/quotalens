"""The plan, read from /api/bootstrap: labels, the active-org pick, the daily refresh.

Bootstrap fragments are hand-written in the shape verified on 2026-10-02 (a Max
5x account): ``account.memberships[].organization`` carries ``capabilities``,
``rate_limit_tier`` and ``billing_type``. Pro, Team, Enterprise and Free shapes are
assumed from that one, not observed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from conftest import ORG, FakeRequest, json_response, make_client, make_handler
from quotalens import plan
from quotalens.poller import Poller
from quotalens.secrets import Redactor

OTHER = "org-9999-8888-ffff"


def _org(uuid: str, capabilities: list[str], tier: str | None, billing: str | None = None):
    return {
        "uuid": uuid,
        "id": 12345,
        "name": "Somebody's Organization",
        "capabilities": capabilities,
        "rate_limit_tier": tier,
        "billing_type": billing,
    }


def _bootstrap(*orgs: dict, last_active: str | None = None) -> dict:
    account: dict = {
        "uuid": "acct-1111-2222-3333",
        "email_address": "someone@example.com",
        "full_name": "Some One",
        "memberships": [{"role": "admin", "seat_tier": None, "organization": o} for o in orgs],
    }
    if last_active:
        account["lastActiveOrgId"] = last_active
    return {"account": account, "statsig": {"user": {"email": "someone@example.com"}}}


MAX_20X = _org(ORG, ["chat", "claude_max"], "default_claude_max_20x", "stripe_subscription")
MAX_5X = _org(ORG, ["chat", "claude_max"], "default_claude_max_5x", "stripe_subscription")
PRO = _org(ORG, ["chat", "claude_pro"], "default_claude_pro", "stripe_subscription")
FREE = _org(ORG, ["chat"], "default_claude_ai", None)
TEAM = _org(ORG, ["chat", "claude_team"], "default_claude_team", "stripe_subscription")
ENTERPRISE = _org(ORG, ["chat", "claude_enterprise"], "enterprise_tier", "invoice")
UNKNOWN = _org(ORG, ["chat", "claude_ultra"], "default_claude_ultra_9x", "stripe_subscription")
API_ORG = _org(OTHER, ["api", "api_individual"], "auto_trust_tier_c", "prepaid")


@pytest.mark.parametrize(
    ("org", "label"),
    [
        (MAX_20X, "Max 20x"),
        (MAX_5X, "Max 5x"),
        (_org(ORG, ["chat", "claude_max"], "default_claude_max"), "Max"),
        (PRO, "Pro"),
        (FREE, "Free"),
        (TEAM, "Team"),
        (ENTERPRISE, "Enterprise"),
        (UNKNOWN, "default_claude_ultra_9x"),  # a plan we cannot name shows its raw tier
    ],
)
def test_label_for_each_plan(org: dict, label: str) -> None:
    found = plan.from_bootstrap(_bootstrap(org), ORG)
    assert found is not None
    assert found.label == label
    assert found.tier == org["rate_limit_tier"]
    assert list(found.capabilities) == org["capabilities"]
    assert found.billing_type == org["billing_type"]


def test_active_org_is_the_second_membership() -> None:
    # The real account looks like this: an API org first, the claude.ai org second.
    found = plan.from_bootstrap(_bootstrap(API_ORG, MAX_5X), ORG)
    assert found is not None and found.label == "Max 5x"
    other = plan.from_bootstrap(_bootstrap(API_ORG, MAX_5X), OTHER)
    assert other is not None and other.label == "auto_trust_tier_c"


def test_org_falls_back_to_last_active_then_to_a_single_membership() -> None:
    assert plan.from_bootstrap(_bootstrap(API_ORG, PRO, last_active=ORG), None).label == "Pro"
    assert plan.from_bootstrap(_bootstrap(PRO), None).label == "Pro"
    assert plan.from_bootstrap(_bootstrap(API_ORG, PRO), None) is None  # never guesses


@pytest.mark.parametrize(
    "data",
    [None, [], {}, {"account": None}, {"account": {"memberships": "x"}}, _bootstrap(API_ORG)],
)
def test_no_plan_when_the_org_is_not_there(data) -> None:
    assert plan.from_bootstrap(data, ORG) is None


def test_label_for_without_anything_is_none() -> None:
    assert plan.label_for((), None) is None
    assert plan.label_for(("api",), None) is None


def test_as_dict_is_the_health_shape_and_carries_nothing_personal() -> None:
    found = plan.from_bootstrap(_bootstrap(API_ORG, MAX_20X), ORG)
    assert found.as_dict() == {
        "label": "Max 20x",
        "tier": "default_claude_max_20x",
        "capabilities": ["chat", "claude_max"],
    }


def test_store_round_trip_and_one_row(store) -> None:
    assert plan.stored(store) is None
    found = plan.from_bootstrap(_bootstrap(MAX_5X), ORG)
    store.write_plan(1, found.label, found.tier, found.capabilities_json(), "x", None)
    store.write_plan(2, found.label, found.tier, found.capabilities_json(), "y", None)
    assert store.query("SELECT COUNT(*) FROM plan")[0][0] == 1
    assert plan.stored(store) == plan.Plan("Max 5x", found.tier, found.capabilities, "y")
    store.write_plan(3, None, None, None, None, None)
    assert plan.stored(store) is None


def test_due_is_daily() -> None:
    assert plan.due(None, 100)
    assert not plan.due(100, 100 + plan.REFRESH_INTERVAL_S - 1)
    assert plan.due(100, 100 + plan.REFRESH_INTERVAL_S)


# -- the poller ------------------------------------------------------------------


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _counting(bootstrap: dict, seen: list[str], status: int = 200):
    base = make_handler(bootstrap=bootstrap)

    def handler(request: FakeRequest):
        seen.append(request.url.path)
        if request.url.path == "/api/bootstrap" and status != 200:
            return json_response(status, {"error": "nope"})
        return base(request)

    return handler


def _poller(settings, store, secrets, handler, clock):
    return Poller(
        settings,
        store,
        secrets,
        Redactor(),
        client_factory=lambda cookie: make_client(handler, cookie),
        clock=clock,
    )


def test_poller_fetches_the_plan_at_start_then_daily(settings, store, secrets) -> None:
    seen: list[str] = []
    clock = Clock(1_000_000.0)
    poller = _poller(settings, store, secrets, _counting(_bootstrap(API_ORG, MAX_5X), seen), clock)

    asyncio.run(poller.poll_once())
    assert seen.count("/api/bootstrap") == 1
    assert plan.stored(store).label == "Max 5x"
    assert store.read_plan().checked_ts == 1_000_000

    clock.now += 3600
    asyncio.run(poller.poll_once())
    assert seen.count("/api/bootstrap") == 1  # never per poll

    clock.now = 1_000_000 + plan.REFRESH_INTERVAL_S
    asyncio.run(poller.poll_once())
    assert seen.count("/api/bootstrap") == 2
    asyncio.run(poller.stop())


def test_a_restart_asks_again_even_inside_the_day(settings, store, secrets) -> None:
    seen: list[str] = []
    handler = _counting(_bootstrap(MAX_5X), seen)
    first = _poller(settings, store, secrets, handler, Clock(1_000_000.0))
    asyncio.run(first.poll_once())
    asyncio.run(first.stop())
    second = _poller(settings, store, secrets, handler, Clock(1_000_060.0))
    asyncio.run(second.poll_once())
    assert seen.count("/api/bootstrap") == 2
    asyncio.run(second.stop())


def test_nothing_from_bootstrap_is_stored_but_the_three_fields(settings, store, secrets) -> None:
    seen: list[str] = []
    poller = _poller(settings, store, secrets, _counting(_bootstrap(MAX_20X), seen), Clock(1e6))
    asyncio.run(poller.poll_once())
    sources = {row[0] for row in store.query("SELECT DISTINCT source FROM sample")}
    assert "bootstrap" not in sources
    dumped = json.dumps([list(r) for r in store.query("SELECT * FROM plan")])
    for personal in ("someone@example.com", "Some One", "Somebody", "acct-1111", ORG, "12345"):
        assert personal not in dumped
    for sample in store.query("SELECT payload FROM sample"):
        assert "someone@example.com" not in sample[0]
    asyncio.run(poller.stop())


def test_a_failed_refresh_keeps_the_plan_and_never_fails_the_poll(settings, store, secrets) -> None:
    seen: list[str] = []
    clock = Clock(1_000_000.0)
    good = _poller(settings, store, secrets, _counting(_bootstrap(PRO), seen), clock)
    asyncio.run(good.poll_once())
    asyncio.run(good.stop())

    bad = _poller(settings, store, secrets, _counting(_bootstrap(PRO), seen, status=500), clock)
    clock.now += 60
    asyncio.run(bad.poll_once())
    assert bad.status.state == "ok"
    assert plan.stored(store).label == "Pro"
    row = store.read_plan()
    assert row.checked_ts == 1_000_060 and row.error == "UpstreamError 500"
    asyncio.run(bad.stop())


def test_refresh_plan_without_a_cookie_returns_what_is_stored(settings, store) -> None:
    from quotalens.secrets import MemorySecretStore

    poller = _poller(settings, store, MemorySecretStore(None), make_handler(), Clock(1e6))
    assert asyncio.run(poller.refresh_plan()) is None


def test_max_multiple_is_read_as_a_number_not_a_substring() -> None:
    assert plan.label_for(["claude_max"], "default_claude_max_15x") == "Max"
    assert plan.label_for(["claude_max"], "default_claude_max_25x") == "Max"
    assert plan.label_for(["claude_max"], "default_claude_max_5x") == "Max 5x"
    assert plan.label_for(["claude_max"], "default_claude_max_20x") == "Max 20x"


def test_label_reads_every_capability_but_only_some_are_kept() -> None:
    caps = ["chat", *(f"feature_{i}" for i in range(40)), "claude_max", 7, None, {"x": 1}]
    found = plan.from_bootstrap(_bootstrap(_org(ORG, caps, "default_claude_max_20x")), ORG)
    assert found.label == "Max 20x"
    assert len(found.capabilities) == plan.MAX_CAPABILITIES
    assert all(isinstance(cap, str) for cap in found.capabilities)


def test_an_error_body_is_never_kept(settings, store, secrets) -> None:
    base = make_handler(bootstrap=_bootstrap(PRO))

    def handler(request: FakeRequest):
        if request.url.path == "/api/bootstrap":
            body = {"error": {"type": "x", "message": "someone@example.com is not allowed"}}
            return json_response(500, body)
        return base(request)

    poller = _poller(settings, store, secrets, handler, Clock(1e6))
    asyncio.run(poller.poll_once())
    assert poller.status.state == "ok"
    assert "someone" not in (store.read_plan().error or "")
    asyncio.run(poller.stop())


def test_a_missing_org_keeps_the_last_good_plan(settings, store, secrets) -> None:
    seen: list[str] = []
    clock = Clock(1_000_000.0)
    good = _poller(settings, store, secrets, _counting(_bootstrap(MAX_5X), seen), clock)
    asyncio.run(good.poll_once())
    asyncio.run(good.stop())
    clock.now += 60
    gone = _poller(settings, store, secrets, _counting(_bootstrap(API_ORG), seen), clock)
    asyncio.run(gone.poll_once())
    assert plan.stored(store).label == "Max 5x"
    assert store.read_plan().error == "active organization not in /api/bootstrap"
    asyncio.run(gone.stop())


def test_a_store_failure_in_the_plan_path_never_fails_the_poll(
    settings, store, secrets, monkeypatch
) -> None:
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "write_plan", broken)
    monkeypatch.setattr(store, "read_plan", broken)
    poller = _poller(settings, store, secrets, make_handler(bootstrap=_bootstrap(PRO)), Clock(1e6))
    asyncio.run(poller.poll_once())
    assert poller.status.state == "ok" and poller.status.polls_ok == 1
    clock_later = Clock(1e6 + 60)
    poller._clock = clock_later
    asyncio.run(poller.poll_once())  # the daily gate's read fails too
    assert poller.status.state == "ok" and poller.status.polls_ok == 2
    asyncio.run(poller.stop())
