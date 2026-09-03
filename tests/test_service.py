"""Background service: pid lifecycle, status exit codes, service installers, log tail."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from quotalens import service
from quotalens.service import (
    Actions,
    ServiceError,
    install_service,
    pid_alive,
    running_pid,
    service_installed,
    service_status,
    start,
    status,
    stop,
    tail,
    uninstall_service,
)


def sleeper(cmd, logfile: Path) -> int:
    """Stand-in spawner: a real detached child that just sleeps."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    logfile.parent.mkdir(parents=True, exist_ok=True)
    logfile.write_text("spawned " + " ".join(cmd) + "\n")
    return proc.pid


def dead_spawner(cmd, logfile: Path) -> int:
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(3)"])
    proc.wait()
    logfile.parent.mkdir(parents=True, exist_ok=True)
    logfile.write_text("boom: config error\n")
    return proc.pid


# -- pid lifecycle --------------------------------------------------------------


def test_start_stop_lifecycle_and_double_start(tmp_path) -> None:
    result = start(tmp_path, ["--interval", "30"], spawner=sleeper, timeout_s=0.1)
    try:
        assert pid_alive(result.pid)
        assert service.read_pid(result.pidfile) == result.pid
        assert "--log-file" in result.log.read_text()
        with pytest.raises(ServiceError, match="already running"):
            start(tmp_path, spawner=sleeper, timeout_s=0.1)
    finally:
        stopped = stop(tmp_path, timeout_s=5)
    assert stopped == result.pid
    assert not pid_alive(result.pid)
    assert not result.pidfile.exists()
    assert stop(tmp_path) is None  # nothing running: no error


def test_stale_pid_file_is_cleaned_and_start_proceeds(tmp_path) -> None:
    pidfile = service.pid_path(tmp_path)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text("99999999\n")  # not a live process
    assert running_pid(pidfile) is None
    assert not pidfile.exists()
    pidfile.write_text("99999999\n")
    result = start(tmp_path, spawner=sleeper, timeout_s=0.1)
    try:
        assert result.pid != 99999999 and pid_alive(result.pid)
    finally:
        stop(tmp_path, timeout_s=5)


def test_start_reports_immediate_exit_with_log_tail(tmp_path) -> None:
    with pytest.raises(ServiceError, match="config error"):
        start(tmp_path, spawner=dead_spawner, timeout_s=0.1)
    assert not service.pid_path(tmp_path).exists()


def silent_sleeper(cmd, logfile: Path) -> int:
    """A child that starts but never says anything. It must not count as ready."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    logfile.parent.mkdir(parents=True, exist_ok=True)
    logfile.touch()
    return proc.pid


def test_start_returns_as_soon_as_the_child_logs(tmp_path) -> None:
    """The wait is a poll, not a fixed sleep: a slow child is waited for, a fast one is not."""

    def late_talker(cmd, logfile: Path) -> int:
        pid = silent_sleeper(cmd, logfile)
        threading.Timer(0.4, lambda: logfile.write_text("ready\n")).start()
        return pid

    began = time.monotonic()
    result = start(tmp_path, spawner=late_talker, timeout_s=20.0)
    elapsed = time.monotonic() - began
    try:
        assert result.ready
        assert 0.3 < elapsed < 10.0, elapsed  # it waited for the line, and did not wait the timeout
    finally:
        stop(tmp_path, timeout_s=5)


def test_start_says_so_when_the_child_stays_silent(tmp_path) -> None:
    result = start(tmp_path, spawner=silent_sleeper, timeout_s=0.2)
    try:
        assert pid_alive(result.pid)  # alive, so not an error
        assert not result.ready  # but nothing to show for it
    finally:
        stop(tmp_path, timeout_s=5)


def test_readiness_ignores_log_lines_from_an_earlier_run(tmp_path) -> None:
    """The log is append-only across runs, so readiness is new bytes, not any bytes."""
    logfile = service.log_path(tmp_path)
    logfile.parent.mkdir(parents=True, exist_ok=True)
    logfile.write_text("2026-01-01 INFO quotalens: from last week\n")
    result = start(tmp_path, spawner=silent_sleeper, timeout_s=0.2)
    try:
        assert not result.ready
    finally:
        stop(tmp_path, timeout_s=5)


def test_acquire_pid_file_refuses_second_instance(tmp_path) -> None:
    pidfile = tmp_path / "x.pid"
    service.acquire_pid_file(pidfile)
    assert service.read_pid(pidfile) == os.getpid()
    service.acquire_pid_file(pidfile)  # our own pid is fine
    start(tmp_path, spawner=sleeper, timeout_s=0.1)
    try:
        with pytest.raises(ServiceError, match="already running"):
            service.acquire_pid_file(service.pid_path(tmp_path))
    finally:
        stop(tmp_path, timeout_s=5)
    service.release_pid_file(pidfile)
    assert not pidfile.exists()


# -- status ---------------------------------------------------------------------


def _health(kind: str, state: str = "ok", **extra):
    return {
        "started_ts": 1_000,
        "collector": {"kind": kind, "message": extra.get("message", "")},
        "poller": {
            "state": state,
            "last_success_ts": 1_800,
            "last_attempt_ts": 1_850,
            "next_poll_ts": 1_900,
            "last_error": extra.get("error"),
        },
    }


def _fetcher(health, current=None):
    def fetch(url: str):
        if url.endswith("/api/health"):
            if isinstance(health, Exception):
                raise health
            return health
        return current or {"readings": [{"label": "5-hour", "pct": 12.0}]}

    return fetch


def test_status_exit_codes(tmp_path) -> None:
    unreachable = urllib.error.URLError("refused")
    assert status(tmp_path, 8787, fetch=_fetcher(unreachable), now=2_000).exit_code == 1
    healthy = status(tmp_path, 8787, fetch=_fetcher(_health("ok")), now=2_000)
    assert healthy.exit_code == 0
    assert any("5-hour" in line for line in healthy.lines)
    assert any("uptime: 0h 16m" in line for line in healthy.lines)
    stale = status(tmp_path, 8787, fetch=_fetcher(_health("stale")), now=2_000)
    assert stale.exit_code == 2
    keyring = status(
        tmp_path,
        8787,
        fetch=_fetcher(_health("auth", "keyring_error", message="Could not read the keyring")),
        now=2_000,
    )
    assert keyring.exit_code == 2
    assert any("Could not read the keyring" in line for line in keyring.lines)


def test_status_with_live_pid_but_dead_http(tmp_path) -> None:
    result = start(tmp_path, spawner=sleeper, timeout_s=0.1)
    try:
        report = status(tmp_path, 8787, fetch=_fetcher(urllib.error.URLError("nope")))
        assert report.exit_code == 1
        assert f"pid {result.pid}" in report.lines[0]
    finally:
        stop(tmp_path, timeout_s=5)


# -- service installers ---------------------------------------------------------


def recorder():
    calls: list[list[str]] = []

    def run(cmd):
        calls.append(list(cmd))
        return 0, ""

    return calls, run


def test_install_macos_writes_plist_and_bootstraps(tmp_path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    calls, run = recorder()
    actions = install_service("darwin", home, data, 45, runner=run, python="/usr/bin/python3")
    plist = home / "Library" / "LaunchAgents" / "com.quotalens.agent.plist"
    assert actions.written == [plist]
    text = plist.read_text()
    assert "<string>/usr/bin/python3</string>" in text and "<string>45</string>" in text
    assert "<key>KeepAlive</key>\n  <true/>" in text and "RunAtLoad" in text
    assert calls[-1][:2] == ["launchctl", "bootstrap"] and calls[-1][-1] == str(plist)
    assert any("Keychain" in n for n in actions.notes)
    assert service_installed("darwin", home, data) == plist
    assert "launchd: loaded" in service_status("darwin", home, data, runner=run)[1]
    removed = uninstall_service("darwin", home, data, runner=run)
    assert removed.removed == [plist] and not plist.exists()
    assert calls[-1][:2] == ["launchctl", "bootout"]


def test_install_linux_writes_unit_and_mentions_linger(tmp_path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    calls, run = recorder()
    actions = install_service("linux", home, data, 60, runner=run, python="/usr/bin/python3")
    unit = home / ".config" / "systemd" / "user" / "quotalens.service"
    assert actions.written == [unit]
    text = unit.read_text()
    assert "Restart=on-failure" in text and "ExecStart=/usr/bin/python3 -m quotalens serve" in text
    assert ["systemctl", "--user", "enable", "--now", "quotalens.service"] in calls
    assert any("loginctl enable-linger" in n for n in actions.notes)
    uninstall_service("linux", home, data, runner=run)
    assert not unit.exists()


def test_install_windows_refuses_and_points_at_the_alternative(tmp_path) -> None:
    """Documentation shaped like code was worse than documentation."""
    home, data = tmp_path / "home", tmp_path / "data"
    calls, run = recorder()
    with pytest.raises(ServiceError, match="Task Scheduler"):
        install_service("win32", home, data, 60, runner=run, python="C:\\py\\python.exe")
    with pytest.raises(ServiceError):
        uninstall_service("win32", home, data, runner=run)
    assert calls == [] and not list(data.glob("*")) if data.exists() else True
    assert service_installed("win32", home, data) is None
    assert service_status("win32", home, data, runner=run) == ["service: not installed"]


def test_install_failure_surfaces_command_output(tmp_path) -> None:
    def failing(cmd):
        return (1, "Bootstrap failed: 5: Input/output error") if "bootstrap" in cmd else (0, "")

    with pytest.raises(ServiceError, match="Input/output error"):
        install_service("darwin", tmp_path, tmp_path / "d", 60, runner=failing)


def test_unknown_platform_is_an_error(tmp_path) -> None:
    with pytest.raises(ServiceError):
        install_service("plan9", tmp_path, tmp_path, 60, runner=lambda c: (0, ""))


# -- logs -----------------------------------------------------------------------


def test_tail_returns_last_lines(tmp_path) -> None:
    path = tmp_path / "q.log"
    path.write_text("".join(f"line {i}\n" for i in range(1000)))
    assert tail(path, 3) == ["line 997", "line 998", "line 999"]
    assert tail(tmp_path / "missing.log", 3) == []


def test_actions_records_commands() -> None:
    calls, run = recorder()
    actions = Actions()
    actions.run(run, ["echo", "hi"])
    assert actions.commands == ["echo hi"] and calls == [["echo", "hi"]]


def test_serve_command_puts_data_dir_before_the_subcommand(tmp_path) -> None:
    cmd = service.serve_command("/py", ["--port", "8790"], data_dir=tmp_path)
    assert cmd == ["/py", "-m", "quotalens", "--data-dir", str(tmp_path), "serve", "--port", "8790"]


def test_start_child_receives_data_dir(tmp_path) -> None:
    seen: list[list[str]] = []

    def spy(cmd, logfile):
        seen.append(list(cmd))
        return sleeper(cmd, logfile)

    start(tmp_path, spawner=spy, timeout_s=0.1)
    try:
        assert seen[0][3:5] == ["--data-dir", str(tmp_path)]
    finally:
        stop(tmp_path, timeout_s=5)


def test_status_uses_the_recorded_port_when_none_is_given(tmp_path) -> None:
    service.write_runtime(tmp_path, 8790, 30)
    seen: list[str] = []

    def fetch(url: str):
        seen.append(url)
        raise urllib.error.URLError("refused")

    status(tmp_path, None, fetch=fetch)
    assert seen and seen[0].startswith("http://127.0.0.1:8790/")
    assert service.read_runtime(tmp_path) == {
        "pid": os.getpid(),
        "port": 8790,
        "interval_s": 30,
        "profile": "",
    }
    service.clear_runtime(tmp_path)
    assert service.read_runtime(tmp_path) is None
    assert status(tmp_path, None, fetch=fetch).exit_code == 1  # nothing recorded, no pid
    assert len(seen) == 1  # and no port was probed


def test_status_without_pid_or_runtime_is_not_running_and_never_probes_a_port(tmp_path) -> None:
    seen: list[str] = []

    def fetch(url: str):
        seen.append(url)
        return {}

    report = status(tmp_path, None, fetch=fetch)
    assert report.exit_code == 1 and report.lines[0] == "not running"
    assert seen == []
