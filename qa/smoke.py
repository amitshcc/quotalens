"""End-to-end smoke test, for CI on macOS, Linux and Windows.

Two halves, because they break in different places:

1. **A real server against a fake claude.ai.** Polls, writes rows, and reads
   them back through the API, `/metrics` and an export. It goes through the
   actual CLI entry point, so settings, the store, the poller and uvicorn are
   all exercised; only the keyring is substituted, using the in-memory store the
   unit tests already inject. **CI therefore proves everything except that the
   platform's keyring backend works** — that is stated in the README rather than
   claimed by a green tick.

2. **The CLI lifecycle with no cookie at all.** `start`, `status`, `logs`,
   `stop`, a double start and a stale pid file. This is where Windows actually
   breaks: paths, the pid file, detaching and process termination.

Run it by hand the same way CI does:

    python qa/smoke.py

Exits non-zero on the first failure, with the reason on stderr.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_claude.py"
COOKIE = "sessionKey=smoke-test-not-a-real-cookie; lastActiveOrg=org-smoke"
DEADLINE_S = 90


class SmokeError(AssertionError):
    pass


def free_port() -> int:
    """Ask the OS for a port nothing is using, rather than hoping about a constant."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def check(condition: object, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def wait_for(url: str, predicate, what: str, deadline_s: int = DEADLINE_S):
    """Poll a URL until the predicate holds, or fail saying what we were waiting for."""
    started = time.monotonic()
    last = ""
    while time.monotonic() - started < deadline_s:
        try:
            status, body = get(url)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(1.0)
            continue
        if status == 200:
            try:
                value = predicate(json.loads(body) if body.startswith("{") else body)
            except Exception as exc:  # a malformed body is just "not yet"
                last = f"{type(exc).__name__}: {exc}"
                value = None
            if value:
                return value
            last = body[:200]
        time.sleep(1.0)
    raise SmokeError(f"timed out waiting for {what}; last response: {last}")


def spawn(args: list[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, *args], **kwargs)


def terminate(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def run_cli(args: list[str], data_dir: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "quotalens", "--data-dir", str(data_dir), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


SERVER_SNIPPET = """
import sys
from quotalens.cli import main
from quotalens.secrets import MemorySecretStore
sys.exit(main(sys.argv[1:], secrets=MemorySecretStore({cookie!r})))
"""


def smoke_server(root: Path, env: dict[str, str]) -> None:
    """A real poll cycle end to end: fake upstream, real server, real database."""
    data_dir = root / "server"
    data_dir.mkdir(parents=True, exist_ok=True)
    upstream_port, server_port = free_port(), free_port()
    upstream = spawn([str(FAKE), str(upstream_port)], stdout=subprocess.DEVNULL)
    server = None
    try:
        wait_for(f"http://127.0.0.1:{upstream_port}/mode", lambda b: True, "the fake upstream")
        server_env = dict(env)
        server_env["QUOTALENS_BASE_URL"] = f"http://127.0.0.1:{upstream_port}"
        server_env["QUOTALENS_DB"] = str(data_dir / "smoke.db")
        server = subprocess.Popen(
            [
                sys.executable,
                "-c",
                SERVER_SNIPPET.format(cookie=COOKIE),
                "--data-dir",
                str(data_dir),
                "serve",
                "--port",
                str(server_port),
                "--interval",
                "30",
            ],
            env=server_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        base = f"http://127.0.0.1:{server_port}"

        health = wait_for(
            f"{base}/api/health",
            lambda body: body if body.get("poller", {}).get("polls_ok", 0) >= 1 else None,
            "the first successful poll",
        )
        check(health["poller"]["last_error"] is None, f"poll error: {health['poller']}")
        print(f"  polled: {health['poller']['polls_ok']} ok, state {health['poller']['state']}")

        # forcing a poll gives us a second sample without waiting out the interval
        request = urllib.request.Request(f"{base}/api/poll", data=b"", method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            check(response.status == 200, "forced poll rejected")

        rows = wait_for(
            f"{base}/api/quota/series?hours=1",
            lambda body: body["readings"] if len(body.get("readings", [])) >= 2 else None,
            "two stored readings",
        )
        windows = {row["window"] for row in rows}
        check("five_hour" in windows, f"no session window in {windows}")
        print(f"  stored and read back {len(rows)} readings across {len(windows)} windows")

        status, page = get(f"{base}/")
        check(status == 200 and "<title>QuotaLens</title>" in page, "the dashboard did not render")

        status, metrics = get(f"{base}/metrics")
        check(status == 200, "/metrics did not answer")
        check(metrics.endswith("\n") and not metrics.endswith("\n\n"), "/metrics newline")
        check("# HELP quotalens_up" in metrics and "\nquotalens_up 1" in metrics, "quotalens_up")
        print(f"  /metrics: {len(metrics.splitlines())} lines, up=1")

        status, csv_body = get(f"{base}/api/export.csv?table=quota")
        lines = csv_body.strip().splitlines()
        check(status == 200 and len(lines) >= 3, f"export returned {len(lines)} lines")
        check(lines[0].startswith("ts,window,label,pct"), f"export header: {lines[0]!r}")
        check(COOKIE not in csv_body, "the cookie reached an export")
        print(f"  export.csv: {len(lines) - 1} rows")

        status = get(f"{base}/api/export.json?table=samples")[0]
        check(status == 400, "raw samples were exported without the flag")
    finally:
        terminate(server)
        terminate(upstream)
        if server is not None and server.stdout is not None:
            tail = server.stdout.read()
            if tail and "Traceback" in tail:
                print(tail[-2000:], file=sys.stderr)


def smoke_lifecycle(root: Path, env: dict[str, str]) -> None:
    """start/status/logs/stop, a double start and a stale pid file. No cookie needed."""
    data_dir = root / "lifecycle"
    data_dir.mkdir(parents=True, exist_ok=True)
    lifecycle_env = dict(env)
    lifecycle_env["QUOTALENS_DB"] = str(data_dir / "lifecycle.db")
    lifecycle_env["QUOTALENS_BASE_URL"] = f"http://127.0.0.1:{free_port()}"  # nothing there

    stopped = run_cli(["status"], data_dir, lifecycle_env)
    check(stopped.returncode == 1, f"status before start: rc={stopped.returncode}")
    check("not running" in stopped.stdout, f"status said: {stopped.stdout!r}")

    lifecycle_port = free_port()
    started = run_cli(["start", "--port", str(lifecycle_port)], data_dir, lifecycle_env)
    try:
        check(started.returncode == 0, f"start failed: {started.stdout}{started.stderr}")
        check("started pid" in started.stdout, f"start said: {started.stdout!r}")
        pid_file = data_dir / "quotalens.pid"
        check(pid_file.exists(), "no pid file")
        pid = int(pid_file.read_text().strip())
        print(f"  started pid {pid}")

        again = run_cli(["start", "--port", str(lifecycle_port)], data_dir, lifecycle_env)
        check(again.returncode == 1, "a second start was allowed")
        check(str(pid) in again.stderr or "already running" in again.stderr, again.stderr)

        logs = run_cli(["logs", "-n", "5"], data_dir, lifecycle_env)
        check(logs.returncode == 0 and logs.stdout.strip(), "logs were empty")
        check("QuotaLens" in logs.stdout, f"unexpected log: {logs.stdout!r}")

        running = run_cli(["status"], data_dir, lifecycle_env)
        check(running.returncode in (0, 2), f"status while running: rc={running.returncode}")
        check("running" in running.stdout, f"status said: {running.stdout!r}")
    finally:
        stop = run_cli(["stop"], data_dir, lifecycle_env)
    check(stop.returncode == 0, f"stop failed: {stop.stdout}{stop.stderr}")
    check(not (data_dir / "quotalens.pid").exists(), "the pid file outlived the process")

    (data_dir / "quotalens.pid").write_text("999999999\n")  # a pid that cannot be alive
    cleaned = run_cli(["status"], data_dir, lifecycle_env)
    check(cleaned.returncode == 1, "a stale pid file read as running")
    check(not (data_dir / "quotalens.pid").exists(), "the stale pid file was not cleaned up")
    print("  lifecycle: start, double start, logs, status, stop, stale pid")


def main() -> int:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    with tempfile.TemporaryDirectory(prefix="quotalens-smoke-") as tmp:
        root = Path(tmp)
        for name, step in (("server", smoke_server), ("lifecycle", smoke_lifecycle)):
            print(f"smoke: {name}")
            try:
                step(root, env)
            except SmokeError as exc:
                print(f"FAILED ({name}): {exc}", file=sys.stderr)
                return 1
    print("smoke: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
