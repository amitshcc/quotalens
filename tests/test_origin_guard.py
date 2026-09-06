"""A website the user visits must not be able to write to, or read, the dashboard.

Every case drives the real middleware through ``TestClient`` so the headers are
explicit and the routes are the shipped ones. Reproductions and the adversary
behind each rule: ``docs/SECURITY-AUDIT-2026-09-06.md`` 2, F1-F4.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import make_client, make_handler
from quotalens import origin_guard
from quotalens.api import create_app
from quotalens.config import config_path, read_config_file

EVIL = "https://evil.example"
# Every state-changing route the audit reached, and how to submit to it.
WRITE_ROUTES = ("/settings", "/settings/retention", "/api/poll", "/api/notify/test", "/poll")


@pytest.fixture
def app(settings, store, secrets, tmp_path, monkeypatch):
    """A real app, with the upstream and the notifier stubbed out.

    Neither is what is under test here, and a status code is the whole
    assertion -- but an unstubbed `/api/notify/test` posts a real banner on the
    machine running the suite.
    """
    from quotalens import notify

    monkeypatch.setattr(notify, "send_test", lambda capability: (True, ""))
    return create_app(
        settings,
        store,
        secrets,
        config_dir=tmp_path,
        client_factory=lambda c: make_client(make_handler(), c),
    )


# -- rule 2: cross-site writes ---------------------------------------------------


def test_a_cross_origin_post_cannot_set_the_webhook(app, settings, tmp_path) -> None:
    """F1: this is what turned the dashboard into a permanent exfil beacon."""
    path = config_path("", tmp_path)
    body = {
        "webhook_url": "https://attacker.example/x",
        "interval": "60",
        "lookback": "15",
        "burn_alert": "20",
        "sample_keep": "20000",
        "status_row": "1",
    }
    with TestClient(app) as tc:
        before = read_config_file(path)
        resp = tc.post("/settings", data=body, headers={"Origin": EVIL}, follow_redirects=False)
    assert resp.status_code == 403
    assert "cross-site request refused" in resp.text
    assert read_config_file(path) == before


def test_a_cross_site_post_cannot_shorten_retention(app, settings, tmp_path) -> None:
    """F2: the "irreversible, so confirm first" guard is a form field a forged form supplies."""
    path = config_path("", tmp_path)
    with TestClient(app) as tc:
        before = read_config_file(path)
        resp = tc.post(
            "/settings/retention",
            data={"retention": "1week", "confirm": "1"},
            headers={"Sec-Fetch-Site": "cross-site"},
            follow_redirects=False,
        )
    assert resp.status_code == 403
    assert read_config_file(path) == before


@pytest.mark.parametrize("route", WRITE_ROUTES)
@pytest.mark.parametrize("header", [{"Origin": EVIL}, {"Sec-Fetch-Site": "cross-site"}])
def test_every_write_route_refuses_a_cross_site_submit(app, route, header) -> None:
    with TestClient(app) as tc:
        assert tc.post(route, headers=header, follow_redirects=False).status_code == 403


@pytest.mark.parametrize("route", WRITE_ROUTES)
def test_every_write_route_accepts_the_dashboards_own_origin(app, settings, route) -> None:
    """A plain <form> POST from the dashboard, and app.js's fetch, both land here."""
    with TestClient(app) as tc:
        for header in (
            {"Origin": f"http://127.0.0.1:{settings.port}"},
            {"Sec-Fetch-Site": "same-origin"},
            {"Sec-Fetch-Site": "none"},  # typed into the address bar
        ):
            assert tc.post(route, headers=header, follow_redirects=False).status_code != 403


@pytest.mark.parametrize("route", WRITE_ROUTES)
def test_every_write_route_accepts_a_client_that_sends_neither_header(app, route) -> None:
    """curl, `qa/smoke.py`'s POST /api/poll, urllib. A non-browser is not the adversary."""
    with TestClient(app) as tc:
        assert tc.post(route, follow_redirects=False).status_code != 403


# -- rule 1: the Host allow-list -------------------------------------------------


def test_a_foreign_host_is_refused_which_is_what_stops_dns_rebinding(app) -> None:
    """F4: answering `Host: attacker.example` is the whole precondition."""
    with TestClient(app) as tc:
        resp = tc.get("/api/quota/series", headers={"Host": "attacker.example"})
    assert resp.status_code == 421
    assert "attacker.example" in resp.text and "loopback" in resp.text


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "localhost:8787", "127.0.0.1:8787"])
def test_the_names_this_instance_answers_for(app, host) -> None:
    with TestClient(app) as tc:
        assert tc.get("/api/quota/series", headers={"Host": host}).status_code == 200


def test_a_missing_host_header_is_refused(app, settings) -> None:
    """HTTP/1.1 requires one; every client that reaches this code sends one."""
    refusal = origin_guard.refuse("GET", {}, origin_guard.allowed_hosts(settings.host), 8787)
    assert refusal is not None and refusal[0] == 421 and "absent" in refusal[1]


def test_a_read_with_a_foreign_origin_is_still_a_read(app) -> None:
    """A GET carrying someone else's Origin is a browser navigation, not the CSRF problem.

    Reads are closed by the Host rule above, which has already run by here.
    """
    with TestClient(app) as tc:
        assert tc.get("/", headers={"Origin": EVIL}).status_code == 200


# -- the rules themselves, without a request -------------------------------------


def test_an_ipv6_host_is_not_split_at_its_first_colon() -> None:
    """`[::1]:8787`.split(":")[0] is "[", which matches nothing and would 421 it."""
    assert origin_guard._hostname("[::1]:8787") == "[::1]"
    assert origin_guard._hostname("127.0.0.1:8787") == "127.0.0.1"
    assert origin_guard._hostname("LOCALHOST") == "localhost"


def test_a_sandboxed_iframes_null_origin_is_not_loopback() -> None:
    hosts = origin_guard.allowed_hosts("127.0.0.1")
    headers = {"host": "127.0.0.1:8787", "origin": "null"}
    assert origin_guard.refuse("POST", headers, hosts, 8787)[0] == 403


def test_sec_fetch_site_decides_before_origin_does() -> None:
    """A cross-site request that also sets a loopback Origin is still cross-site."""
    hosts = origin_guard.allowed_hosts("127.0.0.1")
    headers = {
        "host": "127.0.0.1:8787",
        "sec-fetch-site": "cross-site",
        "origin": "http://127.0.0.1:8787",
    }
    assert origin_guard.refuse("POST", headers, hosts, 8787)[0] == 403
