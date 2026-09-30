"""The About page: works with JavaScript off and reports what the check found."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from quotalens import __version__, status, updates
from quotalens.api import create_app
from quotalens.render import ICONS

EVIL = "https://evil.example"
PYPI_NEWER = {"info": {"version": "99.0.0"}}


@pytest.fixture
def app(settings, store, secrets, tmp_path):
    return create_app(settings, store, secrets, config_dir=tmp_path)


@pytest.fixture
def pypi(monkeypatch):
    """What PyPI answers; the test sets ``pypi.answer`` and reads ``pypi.calls``."""

    class Fake:
        answer: object = PYPI_NEWER
        calls = 0

    fake = Fake()

    def fetch(url, timeout_s=5.0, headers=None):
        if url != updates.PYPI_URL:  # the vendor-status watcher shares this function
            return {"status": {"indicator": "none", "description": ""}}
        fake.calls += 1
        if isinstance(fake.answer, Exception):
            raise fake.answer
        return fake.answer

    monkeypatch.setattr(status, "fetch", fetch)
    return fake


def test_about_page_no_js(app) -> None:
    with TestClient(app) as tc:
        res = tc.get("/about")
    html = res.text
    assert res.status_code == 200 and html.startswith("<!doctype html>")
    assert "Local monitor for your Claude subscription quota." in html
    assert "never sends a prompt" in html
    assert __version__ in html and "not checked yet" in html
    assert 'action="/about/check"' in html and "Check for updates" in html
    for label in ("Python", "Installed with", "Data directory", "Database", "Profile"):
        assert f"<label>{label}</label>" in html
    for label in ("Port", "Poll interval", "Schema version", "Website", "Source", "Licence"):
        assert f"<label>{label}</label>" in html
    assert "Unofficial; not affiliated with or endorsed by Anthropic." in html
    assert "#the-terms-stated-plainly" in html
    assert '<script src="/static/app.js">' in html  # enhancement only; nothing inline
    # The icon sprite carries its own hidden-size attribute on every page; the About
    # markup itself must add no inline style or handler.
    assert "style=" not in html.replace(ICONS, "") and "onclick" not in html


def test_about_fragment(app) -> None:
    with TestClient(app) as tc:
        res = tc.get("/about?fragment=1")
    assert res.status_code == 200 and "<html" not in res.text
    assert res.text.startswith('<section class="sset" id="about"')


def test_about_check_post_origin_guarded(app, pypi) -> None:
    with TestClient(app) as tc:
        refused = tc.post("/about/check", headers={"Origin": EVIL}, follow_redirects=False)
        assert refused.status_code == 403 and pypi.calls == 0
        ok = tc.post(
            "/about/check", headers={"Origin": "http://127.0.0.1:8787"}, follow_redirects=False
        )
    assert ok.status_code == 303 and ok.headers["location"] == "/about" and pypi.calls == 1


def test_about_shows_update_available(app, pypi, monkeypatch) -> None:
    monkeypatch.delenv(updates.ENV_OPT_OUT, raising=False)
    with TestClient(app) as tc:
        tc.post("/about/check", follow_redirects=False)
        html = tc.get("/about").text
    assert "99.0.0 available" in html
    assert 'href="https://github.com/amitshcc/quotalens/releases/tag/v99.0.0"' in html
    assert updates.upgrade_command() in html


def test_about_up_to_date_and_unreachable(app, pypi) -> None:
    pypi.answer = {"info": {"version": __version__}}
    with TestClient(app) as tc:
        tc.post("/about/check", follow_redirects=False)
        assert f"{__version__} \u2014 you\u2019re up to date" in tc.get("/about").text
        assert "available" not in tc.get("/about").text


def test_about_failure_is_quiet(app, pypi, store) -> None:
    pypi.answer = status.StatusFetchError("HTTP 503")
    with TestClient(app) as tc:
        tc.post("/about/check", follow_redirects=False)
        html = tc.get("/about").text
    assert re.search(r"Couldn\u2019t reach PyPI at \d\d:\d\d\.", html)
    assert store.recent_events(kind="poll_error") == []  # never an alarm


def test_about_check_is_rate_limited_and_says_so(app, pypi) -> None:
    with TestClient(app) as tc:
        tc.post("/about/check?fragment=1")
        second = tc.post("/about/check?fragment=1")
    assert pypi.calls == 1
    assert "try again in" in second.text


def test_about_no_external_requests_in_html(app) -> None:
    with TestClient(app) as tc:
        html = tc.get("/about").text
    assert not re.findall(r'\bsrc="https?://', html)
    assert not re.findall(r'<link[^>]+href="https?://', html)
    allowed = {
        "https://quotalens.com",
        "https://github.com/amitshcc/quotalens",
        "https://github.com/amitshcc/quotalens/issues/new/choose",
        "https://github.com/amitshcc/quotalens#the-terms-stated-plainly",
    }
    assert set(re.findall(r'href="(https?://[^"]+)"', html)) == allowed


def test_about_csp_matches_the_dashboard(app) -> None:
    with TestClient(app) as tc:
        assert tc.get("/about").headers.get("content-security-policy") == tc.get("/").headers.get(
            "content-security-policy"
        )
