"""The daily update check: version maths, cadence, failure and opt-out. No network."""

from __future__ import annotations

import pytest

from quotalens import status, updates
from quotalens.store import SCHEMA_VERSION, Store, UpdateCheckRow

NOW = 1_000_000


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def asked(monkeypatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    def fake_fetch(url, timeout_s=5.0, headers=None):
        calls.append((url, headers or {}))
        return {"info": {"version": "2.1.0"}}

    monkeypatch.setattr(status, "fetch", fake_fetch)
    return calls


def test_parse_release_and_prerelease() -> None:
    assert updates.parse_release("2.0.1") == (2, 0, 1)
    assert updates.parse_release("1.0") == (1, 0)
    for junk in ("2.0.0a1", "2.0.0b2", "2.0.0rc1", "2.0.0.dev3", "", "latest", "2..0"):
        assert updates.parse_release(junk) is None


def test_is_newer() -> None:
    assert updates.is_newer("2.0.1", "2.0.0")
    assert updates.is_newer("2.1.0", "2.0.9")
    assert updates.is_newer("10.0.0", "9.9.9")  # integers, not strings
    assert not updates.is_newer("2.0.0", "2.0.0")
    assert not updates.is_newer("2.0", "2.0.0")
    assert not updates.is_newer("1.9.9", "2.0.0")
    assert not updates.is_newer("3.0.0rc1", "2.0.0")  # a pre-release is ignored
    assert not updates.is_newer("3.0.0", "2.0.0.dev1")


def test_due_24h(store) -> None:
    assert updates.due(store, NOW)
    store.write_update_check(NOW, "2.0.0", None)
    assert not updates.due(store, NOW + updates.CHECK_INTERVAL_S - 1)
    assert updates.due(store, NOW + updates.CHECK_INTERVAL_S)


def test_not_due_means_no_request(store, asked) -> None:
    store.write_update_check(NOW, "2.0.0", None)
    state = updates.check(store, NOW + 10, current="2.0.0")
    assert asked == [] and state.latest == "2.0.0"


def test_manual_rate_limit_60s(store, asked) -> None:
    updates.check(store, NOW, force=True, current="2.0.0")
    updates.check(store, NOW + 59, force=True, current="2.0.0")
    assert len(asked) == 1
    assert updates.manual_wait(store, NOW + 59) == 1
    updates.check(store, NOW + 60, force=True, current="2.0.0")
    assert len(asked) == 2


def test_manual_bypasses_the_24h_rule(store, asked) -> None:
    store.write_update_check(NOW - 120, "2.0.0", None)
    state = updates.check(store, NOW, force=True, current="2.0.0")
    assert len(asked) == 1 and state.latest == "2.1.0" and state.available


def test_request_carries_only_a_user_agent(store, asked) -> None:
    updates.check(store, NOW, force=True, current="2.0.0")
    url, headers = asked[0]
    assert url == "https://pypi.org/pypi/quotalens/json" and "?" not in url
    assert headers == {"User-Agent": "quotalens/2.0.0 (+https://quotalens.com)"}


def test_fetch_failure_recorded_not_raised(store, monkeypatch) -> None:
    def boom(url, timeout_s=5.0, headers=None):
        raise status.StatusFetchError("HTTP 503")

    monkeypatch.setattr(status, "fetch", boom)
    store.write_update_check(NOW - 100_000, "2.0.0", None)
    state = updates.check(store, NOW, current="2.0.0")
    assert state.error == "HTTP 503" and state.latest == "2.0.0" and state.checked_ts == NOW
    assert store.read_update_check() == UpdateCheckRow(
        NOW, "2.0.0", "HTTP 503"
    )  # retried at the next 24 h slot
    assert not updates.due(store, NOW + 3600)


def test_unusable_answer_is_a_failure(store, monkeypatch) -> None:
    monkeypatch.setattr(status, "fetch", lambda *a, **k: {"info": {"version": "3.0.0rc1"}})
    state = updates.check(store, NOW, force=True, current="2.0.0")
    assert state.error and state.latest is None and not state.available


def test_env_opt_out(monkeypatch) -> None:
    monkeypatch.delenv(updates.ENV_OPT_OUT, raising=False)
    assert updates.enabled(True) and not updates.enabled(False)
    monkeypatch.setenv(updates.ENV_OPT_OUT, "1")
    assert not updates.enabled(True)  # wins over the setting
    monkeypatch.setenv(updates.ENV_OPT_OUT, "0")
    assert updates.enabled(True)


def test_upgrade_command_by_install_method() -> None:
    assert updates.upgrade_command("/Users/a/.local/pipx/venvs/quotalens") == (
        "pipx upgrade quotalens"
    )
    assert updates.upgrade_command("/Users/a/.local/share/uv/tools/quotalens") == (
        "uv tool upgrade quotalens"
    )
    assert updates.upgrade_command("/usr/lib/python3") == "pip install -U quotalens"


def test_update_check_row_roundtrip(store) -> None:
    assert store.read_update_check() is None
    store.write_update_check(NOW, "2.1.0", None)
    store.write_update_check(NOW + 5, "2.1.0", "HTTP 503")  # one row, overwritten
    assert store.read_update_check() == UpdateCheckRow(NOW + 5, "2.1.0", "HTTP 503")
    assert store.query("SELECT COUNT(*) FROM update_check")[0][0] == 1
    assert store.query("SELECT MAX(version) FROM schema_version")[0][0] == SCHEMA_VERSION


def test_update_available_event_once(asked, store, monkeypatch) -> None:
    updates.check(store, NOW, force=True, current="2.0.0")
    updates.check(store, NOW + 100, force=True, current="2.0.0")  # same latest, no second event
    events = store.recent_events(kind="update_available")
    assert [e.detail for e in events] == ["2.0.0 -> 2.1.0"]
    monkeypatch.setattr(status, "fetch", lambda *a, **k: {"info": {"version": "2.2.0"}})
    updates.check(store, NOW + 200, force=True, current="2.0.0")
    details = {e.detail for e in store.recent_events(kind="update_available")}
    assert details == {"2.0.0 -> 2.1.0", "2.0.0 -> 2.2.0"}


def test_no_event_when_up_to_date(asked, store) -> None:
    updates.check(store, NOW, force=True, current="2.1.0")
    assert store.recent_events(kind="update_available") == []


def test_v7_database_migrates(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    s = Store(path)
    s.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE update_check")
    conn.execute("DELETE FROM schema_version WHERE version = ?", (SCHEMA_VERSION,))
    conn.commit()
    conn.close()
    again = Store(path)
    assert again.read_update_check() is None
    assert again.query("SELECT MAX(version) FROM schema_version")[0][0] == SCHEMA_VERSION
    again.close()


# -- the schedule and the setting -----------------------------------------------


def _poller(settings, store, secrets, clock):
    from quotalens.poller import Poller
    from quotalens.secrets import Redactor

    return Poller(settings, store, secrets, Redactor(), clock=clock)


def _run_update_tick(poller, now: int) -> None:
    import asyncio

    async def tick() -> None:
        poller._maybe_check_updates(now)
        if poller._update_task is not None:
            await poller._update_task

    asyncio.run(tick())


def test_poller_checks_once_five_minutes_after_start(settings, store, secrets, asked, monkeypatch):
    monkeypatch.delenv(updates.ENV_OPT_OUT, raising=False)
    poller = _poller(settings, store, secrets, lambda: NOW)
    _run_update_tick(poller, NOW + updates.FIRST_CHECK_DELAY_S - 1)
    assert asked == []  # never inside the first minutes
    _run_update_tick(poller, NOW + updates.FIRST_CHECK_DELAY_S)
    assert len(asked) == 1 and store.read_update_check().latest == "2.1.0"
    _run_update_tick(poller, NOW + updates.FIRST_CHECK_DELAY_S + 60)
    assert len(asked) == 1  # a restart or a later poll does not re-ask inside 24 h


def test_poller_does_not_check_when_off(settings, store, secrets, asked, monkeypatch):
    monkeypatch.delenv(updates.ENV_OPT_OUT, raising=False)
    off = settings.with_overrides(update_check=False)
    poller = _poller(off, store, secrets, lambda: NOW)
    _run_update_tick(poller, NOW + 10_000)
    assert asked == [] and store.read_update_check() is None


def test_poller_does_not_check_when_env_says_no(settings, store, secrets, asked, monkeypatch):
    monkeypatch.setenv(updates.ENV_OPT_OUT, "1")
    poller = _poller(settings, store, secrets, lambda: NOW)
    _run_update_tick(poller, NOW + 10_000)
    assert asked == []


def test_settings_update_check_roundtrip(settings, store, secrets, tmp_path) -> None:
    from starlette.testclient import TestClient

    from quotalens.api import create_app
    from quotalens.config import config_path, read_config_file

    form = {
        "interval": "60",
        "lookback": "15",
        "burn_alert": "20.0",
        "sample_keep": "20000",
        "webhook_url": "",
        "notify_thresholds": "50,75,90",
        "status_vendors": "claude",
        "status_row": "1",
        "notify_credits": "1",
    }
    assert settings.update_check is True
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        page = tc.get("/settings").text
        assert "Check for updates daily" in page
        assert "Asks pypi.org for the latest version once a day. Nothing else is sent." in page
        tc.post("/settings", data=form, follow_redirects=False)  # box unticked
        assert read_config_file(config_path("", tmp_path))["update_check"] is False
        assert app.state.qw.settings.update_check is False
        tc.post("/settings", data={**form, "update_check": "1"}, follow_redirects=False)
        assert read_config_file(config_path("", tmp_path))["update_check"] is True
        assert app.state.qw.settings.update_check is True
