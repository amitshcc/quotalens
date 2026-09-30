"""The daily update check: version maths, cadence, failure and opt-out. No network."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from quotalens import status, updates

NOW = 1_000_000


@dataclass
class Row:
    checked_ts: int | None
    latest: str | None
    error: str | None


class FakeStore:
    def __init__(self) -> None:
        self.row: Row | None = None

    def read_update_check(self) -> Row | None:
        return self.row

    def write_update_check(self, checked_ts: int, latest: str | None, error: str | None) -> None:
        self.row = Row(checked_ts, latest, error)


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


def test_due_24h() -> None:
    store = FakeStore()
    assert updates.due(store, NOW)
    store.write_update_check(NOW, "2.0.0", None)
    assert not updates.due(store, NOW + updates.CHECK_INTERVAL_S - 1)
    assert updates.due(store, NOW + updates.CHECK_INTERVAL_S)


def test_not_due_means_no_request(asked) -> None:
    store = FakeStore()
    store.write_update_check(NOW, "2.0.0", None)
    state = updates.check(store, NOW + 10, current="2.0.0")
    assert asked == [] and state.latest == "2.0.0"


def test_manual_rate_limit_60s(asked) -> None:
    store = FakeStore()
    updates.check(store, NOW, force=True, current="2.0.0")
    updates.check(store, NOW + 59, force=True, current="2.0.0")
    assert len(asked) == 1
    assert updates.manual_wait(store, NOW + 59) == 1
    updates.check(store, NOW + 60, force=True, current="2.0.0")
    assert len(asked) == 2


def test_manual_bypasses_the_24h_rule(asked) -> None:
    store = FakeStore()
    store.write_update_check(NOW - 120, "2.0.0", None)
    state = updates.check(store, NOW, force=True, current="2.0.0")
    assert len(asked) == 1 and state.latest == "2.1.0" and state.available


def test_request_carries_only_a_user_agent(asked) -> None:
    updates.check(FakeStore(), NOW, force=True, current="2.0.0")
    url, headers = asked[0]
    assert url == "https://pypi.org/pypi/quotalens/json" and "?" not in url
    assert headers == {"User-Agent": "quotalens/2.0.0 (+https://quotalens.com)"}


def test_fetch_failure_recorded_not_raised(monkeypatch) -> None:
    def boom(url, timeout_s=5.0, headers=None):
        raise status.StatusFetchError("HTTP 503")

    monkeypatch.setattr(status, "fetch", boom)
    store = FakeStore()
    store.write_update_check(NOW - 100_000, "2.0.0", None)
    state = updates.check(store, NOW, current="2.0.0")
    assert state.error == "HTTP 503" and state.latest == "2.0.0" and state.checked_ts == NOW
    assert store.row == Row(NOW, "2.0.0", "HTTP 503")  # retried at the next 24 h slot
    assert not updates.due(store, NOW + 3600)


def test_unusable_answer_is_a_failure(monkeypatch) -> None:
    monkeypatch.setattr(status, "fetch", lambda *a, **k: {"info": {"version": "3.0.0rc1"}})
    state = updates.check(FakeStore(), NOW, force=True, current="2.0.0")
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
