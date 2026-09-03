"""`--profile NAME`: a second account is a second process, not account switching."""

from __future__ import annotations

import os

import pytest

from quotalens import service
from quotalens.config import (
    DEFAULT_PORT,
    SettingsError,
    default_db_path,
    default_port,
    normalise_profile,
    settings_from_env,
)
from quotalens.secrets import KEYRING_USERNAME, keyring_username


def test_names_are_normalised_and_bounded() -> None:
    assert normalise_profile(None) == "" and normalise_profile("") == ""
    assert normalise_profile("Work") == "work"
    assert normalise_profile("  Client A/B  ") == "client-a-b"
    assert normalise_profile("work_2") == "work_2"
    with pytest.raises(SettingsError):
        normalise_profile("///")
    with pytest.raises(SettingsError):
        normalise_profile("x" * 41)


def test_the_three_namespaced_defaults() -> None:
    assert keyring_username() == KEYRING_USERNAME
    assert keyring_username("work") == f"{KEYRING_USERNAME}:work"
    assert keyring_username("work") != keyring_username("personal")

    assert default_db_path().name == "quotalens.db"
    assert default_db_path("work").name == "quotalens-work.db"

    assert default_port() == DEFAULT_PORT
    assert default_port("work") != DEFAULT_PORT
    assert default_port("work") == default_port("work")  # stable across runs
    assert default_port("work") != default_port("personal")
    assert 8788 <= default_port("work") <= 8887


def test_a_profile_moves_the_defaults_but_the_environment_still_wins(monkeypatch) -> None:
    for key in ("QUOTALENS_PORT", "QUOTALENS_DB", "QUOTALENS_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    work = settings_from_env("work")
    assert work.profile == "work"
    assert work.port == default_port("work") and work.db_path.name == "quotalens-work.db"

    monkeypatch.setenv("QUOTALENS_PROFILE", "personal")
    assert settings_from_env().profile == "personal"
    monkeypatch.setenv("QUOTALENS_PORT", "9999")
    assert settings_from_env("work").port == 9999


def test_each_profile_owns_its_pid_log_and_runtime_files(tmp_path) -> None:
    """Without this, `stop` on one profile kills the other profile's server."""
    assert service.pid_path(tmp_path) != service.pid_path(tmp_path, "work")
    assert service.log_path(tmp_path, "work").name == "quotalens-work.log"
    assert service.runtime_path(tmp_path, "work").name == "quotalens-work.runtime.json"

    service.write_runtime(tmp_path, 8787, 60)
    service.write_runtime(tmp_path, 8791, 30, "work")
    assert service.read_runtime(tmp_path)["port"] == 8787
    assert service.read_runtime(tmp_path, "work")["port"] == 8791


def test_stopping_one_profile_leaves_the_other_running(tmp_path) -> None:
    from test_service import sleeper

    default = service.start(tmp_path, spawner=sleeper, grace_s=0.1)
    work = service.start(tmp_path, spawner=sleeper, grace_s=0.1, profile="work")
    try:
        assert default.pid != work.pid
        assert service.pid_path(tmp_path).exists() and service.pid_path(tmp_path, "work").exists()
        assert service.stop(tmp_path, profile="work") == work.pid
        assert not service.pid_alive(work.pid)
        assert service.pid_alive(default.pid)  # the one nobody asked to stop
    finally:
        service.stop(tmp_path)
        service.stop(tmp_path, profile="work")
    assert not service.pid_alive(default.pid)


def test_the_child_process_inherits_the_profile(tmp_path) -> None:
    seen: list[list[str]] = []

    def spy(cmd, logfile):
        from test_service import sleeper

        seen.append(list(cmd))
        return sleeper(cmd, logfile)

    service.start(tmp_path, ["--interval", "30"], spawner=spy, grace_s=0.1, profile="work")
    try:
        cmd = seen[0]
        assert "--profile" in cmd and cmd[cmd.index("--profile") + 1] == "work"
        assert cmd.index("--profile") < cmd.index("serve")  # a global flag, before the command
        assert str(service.log_path(tmp_path, "work")) in cmd
    finally:
        service.stop(tmp_path, profile="work")


def test_the_installed_service_unit_carries_the_profile(tmp_path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    calls: list[list[str]] = []

    def run(cmd):
        calls.append(list(cmd))
        return 0, ""

    service.install_service("linux", home, data, 60, runner=run, python="/py", profile="work")
    unit = (home / ".config" / "systemd" / "user" / "quotalens.service").read_text()
    assert "--profile work" in unit and "quotalens-work.log" in unit
    assert os.path.basename(str(data)) == "data"
