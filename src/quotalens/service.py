"""Run QuotaLens as a background service, managed from the CLI. Stdlib only.

* PID file and log live in the data directory.
* ``start`` detaches a ``quotalens serve`` child; ``stop`` sends SIGTERM and
  escalates after a timeout; ``status`` reads ``/api/health`` and exits
  non-zero when not running or when the collector has stalled.
* ``service install`` writes a LaunchAgent (macOS) or a systemd user unit
  (Linux). Windows has no user-level service manager we can install into; the
  command says so and points at the README.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from quotalens.config import APP_NAME, DEFAULT_PORT, default_data_dir, profile_suffix

log = logging.getLogger(__name__)

# One set of files per profile, or `stop` on one profile kills the other's server.
PID_FILE = f"{APP_NAME}.pid"
LOG_FILE = f"{APP_NAME}.log"
RUNTIME_FILE = f"{APP_NAME}.runtime.json"  # port and interval of the instance in this data dir
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3
STOP_TIMEOUT_S = 10.0
WINDOWS_GUIDANCE = (
    "Windows has no user-level service manager this command can install into. Run "
    "`quotalens start` at logon instead: create a Task Scheduler task with the "
    "trigger 'At log on' and the action `quotalens start`, or put a shortcut to it "
    "in shell:startup. start/stop/status/logs work the same way on Windows."
)
START_GRACE_S = 1.5
LAUNCHD_LABEL = "com.quotalens.agent"
SYSTEMD_UNIT = "quotalens.service"

Runner = Callable[[Sequence[str]], tuple[int, str]]
Spawner = Callable[[Sequence[str], Path], int]


class ServiceError(RuntimeError):
    """Something the user must act on; the message is the whole explanation."""


# -- paths and pid file ---------------------------------------------------------


def pid_path(data_dir: Path | None = None, profile: str = "") -> Path:
    return (data_dir or default_data_dir()) / f"{APP_NAME}{profile_suffix(profile)}.pid"


def log_path(data_dir: Path | None = None, profile: str = "") -> Path:
    return (data_dir or default_data_dir()) / f"{APP_NAME}{profile_suffix(profile)}.log"


def runtime_path(data_dir: Path | None = None, profile: str = "") -> Path:
    return (data_dir or default_data_dir()) / f"{APP_NAME}{profile_suffix(profile)}.runtime.json"


def write_runtime(data_dir: Path, port: int, interval_s: int, profile: str = "") -> None:
    """Written by ``serve`` so ``status`` and ``restart`` address this instance, not a default."""
    data_dir.mkdir(parents=True, exist_ok=True)
    runtime_path(data_dir, profile).write_text(
        json.dumps({"pid": os.getpid(), "port": port, "interval_s": interval_s, "profile": profile})
    )


def read_runtime(data_dir: Path, profile: str = "") -> dict[str, Any] | None:
    try:
        data = json.loads(runtime_path(data_dir, profile).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_runtime(data_dir: Path, profile: str = "") -> None:
    runtime = read_runtime(data_dir, profile)
    if runtime and runtime.get("pid") == os.getpid():
        runtime_path(data_dir, profile).unlink(missing_ok=True)


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):  # os.kill(pid, 0) would terminate it on Windows
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:  # a child that exited but was not reaped is a zombie, and kill(pid, 0) still succeeds
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass  # not our child; fall through to the signal probe
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_pid(path: Path) -> int | None:
    """The live pid recorded in ``path``; a stale file is removed and reported as None."""
    pid = read_pid(path)
    if pid is None:
        return None
    if pid_alive(pid):
        return pid
    log.info("removing stale pid file %s (pid %d is gone)", path, pid)
    path.unlink(missing_ok=True)
    return None


def acquire_pid_file(path: Path) -> None:
    """Called by ``serve``: refuse to run twice, then record our own pid."""
    other = running_pid(path)
    if other is not None and other != os.getpid():
        raise ServiceError(f"QuotaLens is already running (pid {other}); see `quotalens status`")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n")


def release_pid_file(path: Path) -> None:
    if read_pid(path) == os.getpid():
        path.unlink(missing_ok=True)


# -- logging --------------------------------------------------------------------


def configure_file_logging(path: Path) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    return handler


def tail(path: Path, lines: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        block, data = 4096, b""
        while size > 0 and data.count(b"\n") <= lines:
            step = min(block, size)
            size -= step
            fh.seek(size)
            data = fh.read(step) + data
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]


def follow(path: Path, out: Callable[[str], None], poll_s: float = 0.5) -> None:
    """Print appended lines until interrupted."""
    position = path.stat().st_size if path.exists() else 0
    while True:
        if path.exists():
            with path.open("rb") as fh:
                fh.seek(position)
                chunk = fh.read()
                position = fh.tell()
            if chunk:
                for line in chunk.decode("utf-8", errors="replace").splitlines():
                    out(line)
        time.sleep(poll_s)


# -- start / stop ---------------------------------------------------------------


def serve_command(
    python: str = sys.executable,
    extra: Sequence[str] = (),
    data_dir: Path | None = None,
    profile: str = "",
) -> list[str]:
    """The foreground command. ``--data-dir`` and ``--profile`` are global flags."""
    prefix: list[str] = []
    if data_dir is not None:
        prefix += ["--data-dir", str(data_dir)]
    if profile:
        prefix += ["--profile", profile]
    return [python, "-m", "quotalens", *prefix, "serve", *extra]


def _spawn_detached(cmd: Sequence[str], logfile: Path) -> int:
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with logfile.open("ab") as out:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": out,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(list(cmd), **kwargs).pid


@dataclass
class StartResult:
    pid: int
    log: Path
    pidfile: Path


def start(
    data_dir: Path,
    serve_args: Sequence[str] = (),
    spawner: Spawner = _spawn_detached,
    grace_s: float = START_GRACE_S,
    profile: str = "",
) -> StartResult:
    pidfile, logfile = pid_path(data_dir, profile), log_path(data_dir, profile)
    existing = running_pid(pidfile)
    if existing is not None:
        raise ServiceError(
            f"already running (pid {existing}); use `quotalens restart` to replace it"
        )
    cmd = serve_command(
        extra=[*serve_args, "--log-file", str(logfile)], data_dir=data_dir, profile=profile
    )
    pid = spawner(cmd, logfile)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(f"{pid}\n")
    time.sleep(grace_s)
    if not pid_alive(pid):
        pidfile.unlink(missing_ok=True)
        recent = "\n".join(tail(logfile, 8))
        raise ServiceError(f"the server exited immediately. Last log lines:\n{recent}")
    return StartResult(pid, logfile, pidfile)


def stop(data_dir: Path, timeout_s: float = STOP_TIMEOUT_S, profile: str = "") -> int | None:
    """Terminate the recorded process. Returns the pid stopped, or None if nothing ran."""
    pidfile = pid_path(data_dir, profile)
    pid = running_pid(pidfile)
    if pid is None:
        return None
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    else:
        log.warning("pid %d ignored SIGTERM for %.0fs; killing", pid, timeout_s)
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        time.sleep(0.2)
    pidfile.unlink(missing_ok=True)
    return pid


# -- status ---------------------------------------------------------------------


def port_in_use(host: str, port: int) -> bool:
    """True when something already holds the port. Cheap, and races nobody in practice."""
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def port_conflict_message(host: str, port: int, profile: str, derived: bool = False) -> str:
    """Name the port, the profile and the flag that fixes it. Never a traceback."""
    who = f'the "{profile}" profile' if profile else "QuotaLens"
    derived = (
        f" Nothing chose {port}: it is derived from the profile name {profile!r}, so it"
        " is not obvious what else might want it."
        if derived and profile
        else ""
    )
    flag = f" --profile {profile}" if profile else ""
    return (
        f"{host}:{port} is already in use, so {who} cannot start.{derived}\n"
        f"Another QuotaLens instance, or something unrelated, is on it. Pick another:\n"
        f"    quotalens{flag} start --port {port + 1}"
    )


def fetch_json(url: str, timeout_s: float = 3.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:
        return json.loads(resp.read())


@dataclass
class StatusReport:
    exit_code: int  # 0 healthy, 1 not running, 2 running but stalled or misconfigured
    lines: list[str] = field(default_factory=list)
    port: int | None = None  # the port this instance is actually on


STALLED_KINDS = {"stale", "auth", "unverified"}


def status(
    data_dir: Path,
    port: int | None,
    fetch: Callable[[str], Any] = fetch_json,
    now: float | None = None,
    profile: str = "",
) -> StatusReport:
    """``port`` None means: the port the instance in this data directory recorded."""
    now = now if now is not None else time.time()
    pidfile = pid_path(data_dir, profile)
    pid = running_pid(pidfile)
    runtime = read_runtime(data_dir, profile) or {}
    if port is None:
        if pid is None and not runtime:
            return StatusReport(1, ["not running", f"pid file: {pidfile} (absent)"])
        port = int(runtime.get("port") or DEFAULT_PORT)
    base = f"http://127.0.0.1:{port}"
    try:
        health = fetch(f"{base}/api/health")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if pid is None:
            return StatusReport(1, ["not running", f"pid file: {pidfile} (absent)"], port)
        return StatusReport(
            1, [f"process alive (pid {pid}) but {base}/api/health did not answer: {exc}"], port
        )
    poller = health.get("poller", {})
    collector = health.get("collector", {})
    started = health.get("started_ts")
    lines = [
        "running" + (f" (pid {pid})" if pid else " (pid unknown, no pid file)"),
        f"uptime: {_fmt_duration(now - started) if started else 'unknown'}",
        f"collector: {collector.get('kind', poller.get('state'))}"
        + (f" - {collector.get('message')}" if collector.get("message") else ""),
        f"last successful poll: {_fmt_ts(poller.get('last_success_ts'))}",
        f"last attempt: {_fmt_ts(poller.get('last_attempt_ts'))}",
        f"next poll: {_fmt_ts(poller.get('next_poll_ts'))}",
    ]
    if poller.get("last_error"):
        lines.append(f"last error: {poller['last_error']}")
    try:
        dash = fetch(f"{base}/api/dashboard")
        lines.extend(_session_lines(dash, now))
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        lines.append("session: unavailable")
    try:
        current = fetch(f"{base}/api/quota/current")
        for reading in current.get("readings", []):
            name = reading.get("display") or reading["label"]
            lines.append(f"  {name:<22} {reading['pct']:6.1f}%")
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        lines.append("  (windows unavailable)")
    lines.append(f"log: {log_path(data_dir, profile)}")
    stalled = collector.get("kind") in STALLED_KINDS or poller.get("state") in {
        "keyring_error",
        "no_cookie",
        "auth_expired",
        "blocked",
    }
    return StatusReport(2 if stalled else 0, lines, port)


def _session_lines(dash: dict[str, Any], now: float) -> list[str]:
    runway = dash.get("runway") or {}
    current = next((s for s in dash.get("sessions", []) if s.get("is_current")), None)
    if not current or not runway.get("reset_ts"):
        return ["session: none running"]
    start = time.strftime("%H:%M", time.localtime(current["started_at"]))
    end = time.strftime("%H:%M", time.localtime(current["ends_at"]))
    remaining = max(0, int(runway["reset_ts"] - now))
    headroom = runway.get("headroom_pct")
    head = f", {headroom:.0f}% headroom" if headroom is not None else ""
    return [
        f"session: {start}-{end}, resets in {_fmt_duration(remaining)}{head}",
        f"verdict: {runway.get('verdict', '')}",
    ]


def _fmt_ts(ts: Any) -> str:
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    return (f"{days}d " if days else "") + f"{hours}h {minutes:02d}m"


# -- service managers -----------------------------------------------------------


def _run(cmd: Sequence[str]) -> tuple[int, str]:
    proc = subprocess.run(list(cmd), capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


@dataclass
class Actions:
    """Everything a service command did, so the user can undo it by hand."""

    written: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def run(self, runner: Runner, cmd: Sequence[str]) -> tuple[int, str]:
        self.commands.append(" ".join(cmd))
        return runner(cmd)


def launchd_plist(cmd: Sequence[str], logfile: Path, workdir: Path) -> str:
    args = "".join(f"    <string>{xml_escape(a)}</string>\n" for a in cmd)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key>\n  <string>{LAUNCHD_LABEL}</string>\n"
        f"  <key>ProgramArguments</key>\n  <array>\n{args}  </array>\n"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        "  <key>KeepAlive</key>\n  <true/>\n"
        f"  <key>WorkingDirectory</key>\n  <string>{xml_escape(str(workdir))}</string>\n"
        f"  <key>StandardOutPath</key>\n  <string>{xml_escape(str(logfile))}</string>\n"
        f"  <key>StandardErrorPath</key>\n  <string>{xml_escape(str(logfile))}</string>\n"
        "</dict>\n</plist>\n"
    )


def systemd_unit(cmd: Sequence[str], workdir: Path) -> str:
    exec_start = " ".join(_sh_quote(a) for a in cmd)
    return (
        "[Unit]\nDescription=QuotaLens local Claude usage monitor\nAfter=network-online.target\n\n"
        f"[Service]\nExecStart={exec_start}\nWorkingDirectory={workdir}\n"
        "Restart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n"
    )


def _sh_quote(arg: str) -> str:
    return (
        arg
        if all(c.isalnum() or c in "-_./=:" for c in arg)
        else '"' + arg.replace('"', '\\"') + '"'
    )


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def install_service(
    platform: str,
    home: Path,
    data_dir: Path,
    interval: int,
    runner: Runner = _run,
    python: str = sys.executable,
    profile: str = "",
) -> Actions:
    actions = Actions()
    logfile = log_path(data_dir, profile)
    cmd = serve_command(
        python, ["--interval", str(interval), "--log-file", str(logfile)], profile=profile
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    if platform == "darwin":
        plist = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(launchd_plist(cmd, logfile, data_dir))
        actions.written.append(plist)
        target = f"gui/{_uid()}"
        actions.run(runner, ["launchctl", "bootout", f"{target}/{LAUNCHD_LABEL}"])  # idempotent
        rc, out = actions.run(runner, ["launchctl", "bootstrap", target, str(plist)])
        if rc != 0:
            raise ServiceError(f"launchctl bootstrap failed ({rc}): {out}")
        actions.notes.append(
            "First run may prompt for Keychain access; if the agent cannot read the cookie, "
            "`quotalens status` will say so. Undo with `quotalens service uninstall`."
        )
    elif platform.startswith("linux"):
        unit = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(systemd_unit(cmd, data_dir))
        actions.written.append(unit)
        for c in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT],
        ):
            rc, out = actions.run(runner, c)
            if rc != 0:
                raise ServiceError(f"{' '.join(c)} failed ({rc}): {out}")
        user = os.environ.get("USER", "$USER")
        actions.notes.append(
            "The unit runs only while you are logged in unless lingering is enabled. To keep "
            f"it collecting after logout or a reboot, run: loginctl enable-linger {user}"
        )
    elif platform.startswith("win"):
        raise ServiceError(WINDOWS_GUIDANCE)
    else:
        raise ServiceError(f"no service manager support for platform {platform!r}")
    return actions


def uninstall_service(platform: str, home: Path, data_dir: Path, runner: Runner = _run) -> Actions:
    actions = Actions()
    if platform == "darwin":
        plist = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        actions.run(runner, ["launchctl", "bootout", f"gui/{_uid()}/{LAUNCHD_LABEL}"])
        if plist.exists():
            plist.unlink()
            actions.removed.append(plist)
    elif platform.startswith("linux"):
        unit = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT
        actions.run(runner, ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
        if unit.exists():
            unit.unlink()
            actions.removed.append(unit)
        actions.run(runner, ["systemctl", "--user", "daemon-reload"])
    elif platform.startswith("win"):
        raise ServiceError(WINDOWS_GUIDANCE)
    else:
        raise ServiceError(f"no service manager support for platform {platform!r}")
    return actions


def service_installed(platform: str, home: Path, data_dir: Path) -> Path | None:
    if platform == "darwin":
        p = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    elif platform.startswith("linux"):
        p = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT
    elif platform.startswith("win"):
        return None
    else:
        return None
    return p if p.exists() else None


def service_status(platform: str, home: Path, data_dir: Path, runner: Runner = _run) -> list[str]:
    installed = service_installed(platform, home, data_dir)
    if installed is None:
        return ["service: not installed"]
    lines = [f"service: installed ({installed})"]
    if platform == "darwin":
        rc, out = runner(["launchctl", "print", f"gui/{_uid()}/{LAUNCHD_LABEL}"])
        lines.append(
            "launchd: loaded" if rc == 0 else "launchd: not loaded (bootstrap it or reinstall)"
        )
    elif platform.startswith("linux"):
        rc, out = runner(["systemctl", "--user", "is-active", SYSTEMD_UNIT])
        lines.append(f"systemd: {out or ('active' if rc == 0 else 'inactive')}")
    return lines
