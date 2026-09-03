"""Command line: ``quotalens auth | probe | serve``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from quotalens import __version__, service
from quotalens.client import ClaudeClient, ClientError, has_session_key
from quotalens.config import (
    DEFAULT_SAMPLE_KEEP,
    MIN_POLL_INTERVAL_S,
    Settings,
    SettingsError,
    default_data_dir,
    default_db_path,
    default_port,
    settings_from_env,
    validate,
)
from quotalens.export import RAW_WARNING, mask_uuids
from quotalens.parse import ParseError, parse_spend, parse_usage
from quotalens.secrets import (
    KeyringSecretStore,
    SecretStore,
    SecretStoreError,
    global_redactor,
    install_log_redaction,
)

log = logging.getLogger("quotalens")

PROBE_WARNING = (
    "!! WARNING: the output below is your account's usage data. UUID-shaped values\n"
    "!! are masked unless --no-redact is given. " + RAW_WARNING + "\n"
)


def _setup_logging(verbose: bool, log_file: Path | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if log_file is None:
        logging.basicConfig(level=level, format=fmt)
    else:
        # Detached: stderr is redirected into the same file, so the file handler is the
        # only handler; otherwise every line would appear twice.
        logging.basicConfig(level=level, format=fmt, handlers=[])
        service.configure_file_logging(log_file)
    install_log_redaction()


def _load_cookie(secrets: SecretStore) -> str:
    cookie = secrets.get_cookie()
    if not cookie:
        raise SystemExit("no session cookie stored; run `quotalens auth` first")
    global_redactor().add(cookie)
    return cookie


def read_hidden_line(prompt: str, stream: IO[Any] | None = None) -> str:
    """Read one line without echo and without the terminal's line-length cap.

    ``getpass`` reads in canonical mode, where macOS drops everything past 1024
    bytes on a line, including the newline, so a pasted Cookie header hangs the
    prompt. On a POSIX tty we switch to cbreak mode and read chunks ourselves.
    Piped stdin (``pbpaste | quotalens auth``) is read directly.
    """
    stream = stream or sys.stdin
    if not stream.isatty():
        return stream.readline().rstrip("\r\n")
    if sys.platform.startswith("win"):
        return getpass.getpass(prompt)

    import termios
    import tty

    fd = stream.fileno()
    saved = termios.tcgetattr(fd)
    sys.stdout.write(prompt)
    sys.stdout.flush()
    buf = bytearray()
    try:
        tty.setcbreak(fd)  # no echo, no line buffering; Ctrl-C still raises
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            done = False
            for byte in chunk:
                if byte in (10, 13):  # \n or \r ends the line
                    done = True
                    break
                if byte in (8, 127):  # backspace
                    if buf:
                        buf.pop()
                    continue
                if byte == 4:  # Ctrl-D
                    done = True
                    break
                buf.append(byte)
            if done:
                break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\n")
        sys.stdout.flush()
    return buf.decode("utf-8", errors="replace")


async def _verify(cookie: str, settings: Settings) -> tuple[object, object | None]:
    async with ClaudeClient(
        cookie,
        base_url=settings.base_url,
        timeout_s=settings.http_timeout_s,
        user_agent=settings.user_agent,
        impersonate=settings.impersonate,
    ) as client:
        usage = await client.fetch_usage()
        try:
            overage = await client.fetch_overage()
        except ClientError:
            overage = None
        return usage, overage


def cmd_auth(args: argparse.Namespace, settings: Settings, secrets: SecretStore) -> int:
    # Ask the keyring first. Otherwise a headless box makes the user paste a cookie
    # and wait for a network round trip before telling them it cannot store one.
    try:
        secrets.get_cookie()
    except SecretStoreError as exc:
        print(f"cannot use the keyring: {exc}", file=sys.stderr)
        return 3
    if sys.stdin.isatty():
        print(
            "Paste your claude.ai session cookie (the full Cookie header value from a\n"
            "request to claude.ai/settings/usage), then press Enter. Input is hidden.\n"
            "Tip: `pbpaste | quotalens auth` (macOS) also works."
        )
    try:
        cookie = read_hidden_line("Cookie: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\naborted", file=sys.stderr)
        return 1
    if not cookie:
        print("no value entered", file=sys.stderr)
        return 1
    global_redactor().add(cookie)
    if not has_session_key(cookie):
        print(
            "warning: no `sessionKey=` pair found; claude.ai usually needs it. Continuing.",
            file=sys.stderr,
        )
    print("Verifying with one request to claude.ai ...")
    try:
        usage, _ = asyncio.run(_verify(cookie, settings))
        readings = parse_usage(usage).readings
    except ClientError as exc:
        print(f"verification failed: {global_redactor().redact(str(exc))}", file=sys.stderr)
        if not args.force:
            print("Cookie NOT stored. Re-run with --force to store it anyway.", file=sys.stderr)
            return 2
        print("Storing anyway because --force was given.", file=sys.stderr)
        readings = []
    except ParseError as exc:
        print(
            f"warning: authenticated, but the payload could not be parsed: {exc}", file=sys.stderr
        )
        readings = []
    try:
        secrets.set_cookie(cookie)
    except SecretStoreError as exc:
        print(f"could not store the cookie: {exc}", file=sys.stderr)
        return 3
    print(f"Verified and stored in the OS keyring ({len(readings)} quota windows visible).")
    return 0


def cmd_probe(args: argparse.Namespace, settings: Settings, secrets: SecretStore) -> int:
    cookie = _load_cookie(secrets)
    try:
        usage, overage = asyncio.run(_verify(cookie, settings))
    except ClientError as exc:
        print(f"request failed: {global_redactor().redact(str(exc))}", file=sys.stderr)
        return 2
    redact = (lambda text: text) if args.no_redact else mask_uuids
    print(PROBE_WARNING)
    print("== raw /usage ==")
    print(redact(json.dumps(usage, indent=2, sort_keys=True)))
    print("\n== raw /overage_spend_limit ==")
    overage_text = json.dumps(overage, indent=2, sort_keys=True) if overage is not None else None
    print(redact(overage_text) if overage_text else "(unavailable)")
    print("\n== parsed ==")
    try:
        parsed = parse_usage(usage)
    except ParseError as exc:
        print(f"PARSE FAILED: {exc}")
        return 4
    for r in parsed.readings:
        reset = f"resets {r.resets_at}" if r.resets_at else "no reset time"
        flags = " ".join(f for f in (r.severity or "", "active" if r.is_active else "") if f)
        print(f"  {r.window:<24} {r.label:<16} {r.pct:6.1f}%  {reset}  {flags}")
    for block in parsed.ignored:
        print(f"  (ignored) {block.key}: {block.reason}")
    spend = parse_spend(usage, overage)
    if spend is not None:
        pct = f"{spend.pct:.0f}%" if spend.pct is not None else "n/a"
        money = (
            f"{spend.used_text} / {spend.limit_text}" if spend.used_text else "(figures suppressed)"
        )
        state = (
            "enabled" if spend.is_enabled else f"disabled ({spend.disabled_reason or 'no reason'})"
        )
        until = f" until {spend.disabled_until}" if spend.disabled_until else ""
        print(f"  extra usage: {money}  {pct}  {state}{until}  [source: {spend.source}]")
    if parsed.fallback_used:
        print("\nNOTE: parsed via generic fallback; the endpoint shape may have drifted.")
    return 0


def _profile_note(settings: Settings) -> str:
    """A named profile is invisible otherwise, and its port is derived, not chosen."""
    return f' (profile "{settings.profile}")' if settings.profile else ""


def cmd_prune(args: argparse.Namespace, settings: Settings, secrets: SecretStore) -> int:
    from quotalens.store import Store

    keep = args.keep or settings.sample_keep
    store = Store(settings.db_path)
    try:
        result = store.prune_samples(keep, dry_run=args.dry_run)
    finally:
        store.close()
    count = result.candidates if args.dry_run else result.deleted
    verb = "would remove" if args.dry_run else "removed"
    print(f"{settings.db_path}")
    print(f"{verb} {count} raw samples; {result.kept} kept (limit {keep})")
    print(f"payload shapes preserved: {result.signatures}")
    if result.bytes_before is not None and result.bytes_after is not None:
        print(
            f"file: {result.bytes_before / 1_048_576:.1f} MB -> "
            f"{result.bytes_after / 1_048_576:.1f} MB"
        )
    return 0


# Commands that take --db. Missing one means the flag is silently ignored, which
# for a command that deletes rows would mean deleting them from the wrong file.
DB_FLAG_COMMANDS = ("serve", "prune", "forget")


def _local_stamp(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


SESSION_LIST_LIMIT = 5000  # about three years of five-hour windows


def _session_rows(store: Any) -> tuple[list[dict[str, Any]], bool]:
    """Every window `forget` can act on, oldest first, and whether that is all of them.

    A silent truncation here would be worse than a slow listing: an id that never
    appears cannot be passed back, and `forget` would call a real window unknown.
    """
    rows = store.sessions(limit=SESSION_LIST_LIMIT, order="recent")
    return sorted(rows, key=lambda w: w["started_at"]), len(rows) < SESSION_LIST_LIMIT


BARELY_OBSERVED_RATIO = 0.05  # 15 minutes of a five-hour window


def _forget_listing(rows: list[dict[str, Any]]) -> list[str]:
    """Every window with the id `forget` takes, and a note on the ones worth a look.

    A window another collector left behind shows up as minutes of observation
    inside a five-hour window, because that collector ran for minutes. That is a
    reason to look, not proof: a window where you genuinely only had the collector
    up for two minutes looks the same, and only you know which it was.
    """
    out = []
    for w in rows:
        started, samples = int(w["started_at"]), int(w["samples"])
        span = int(w["last_ts"]) - int(w["first_ts"])
        length = max(1, int(w["ends_at"]) - started)
        marker = "  <- barely observed" if span < length * BARELY_OBSERVED_RATIO else ""
        out.append(
            f"  {started}  {_local_stamp(started)}  {samples:>4} samples  "
            f"peak {float(w['peak_pct']):5.1f}%  {_span(span):>7} observed{marker}"
        )
    return out


def _span(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def cmd_forget(args: argparse.Namespace, settings: Settings, secrets: SecretStore) -> int:
    """Remove session windows that came from somewhere other than this account."""
    from quotalens.sessions import rebuild, window_sample_ts
    from quotalens.store import Store

    store = Store(settings.db_path)
    try:
        rows, complete = _session_rows(store)
        print(f"{settings.db_path}")
        if not complete:
            print(
                f"only the newest {SESSION_LIST_LIMIT} windows are listed; older ones "
                "cannot be named here",
                file=sys.stderr,
            )
        if not args.session:
            print(f"{len(rows)} session windows. Pass one or more ids to remove them:")
            for line in _forget_listing(rows):
                print(line)
            print("\n  quotalens forget <id> [<id>...] --dry-run")
            return 0

        known = {int(w["started_at"]): w for w in rows}
        missing = [s for s in args.session if s not in known]
        if missing:
            print(f"no such session window: {', '.join(str(m) for m in missing)}", file=sys.stderr)
            print("run `quotalens forget` with no arguments to list them", file=sys.stderr)
            return 1

        stamps: list[int] = []
        for started in args.session:
            window = known[started]
            found = window_sample_ts(store, int(window["ends_at"]))
            print(
                f"{started} ({_local_stamp(started)}): {len(found)} samples, "
                f"peak {float(window['peak_pct']):.1f}%"
            )
            stamps.extend(found)

        result = store.forget_samples(stamps, dry_run=args.dry_run)
        verb = "would remove" if args.dry_run else "removed"
        print(
            f"{verb} {result.samples} samples, {result.quota_rows} quota rows, "
            f"{result.overage_rows} overage rows"
        )
        if args.dry_run:
            print("nothing was changed. Re-run without --dry-run to apply.")
            return 0
        count = rebuild(store, int(time.time()))
        print(f"session windows rebuilt from what is left: {count}")
    finally:
        store.close()
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings, secrets: SecretStore) -> int:
    import uvicorn

    from quotalens.api import create_app
    from quotalens.store import Store

    pidfile = service.pid_path(args.data_dir, settings.profile)
    try:
        service.acquire_pid_file(pidfile)
    except service.ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if service.port_in_use(settings.host, settings.port):
        print(
            service.port_conflict_message(
                settings.host,
                settings.port,
                settings.profile,
                derived=settings.port == default_port(settings.profile),
            ),
            file=sys.stderr,
        )
        service.release_pid_file(pidfile)
        return 1
    service.write_runtime(args.data_dir, settings.port, settings.poll_interval_s, settings.profile)
    try:
        cookie = secrets.get_cookie()
    except SecretStoreError as exc:
        cookie = None
        log.error(
            "CANNOT READ THE KEYRING: %s. The poller will report keyring_error until this is "
            "fixed. If this runs as a background service, run `quotalens auth` from a terminal "
            "and grant keychain access to python.",
            exc,
        )
    if cookie:
        global_redactor().add(cookie)
    elif cookie is None and not isinstance(secrets, KeyringSecretStore):
        log.warning("no session cookie stored; the poller will idle until you run `quotalens auth`")
    store = Store(settings.db_path)
    app = create_app(settings, store, secrets)
    print(f"QuotaLens {__version__}{_profile_note(settings)}")
    print(f"dashboard: http://{settings.host}:{settings.port}/")
    print(f"database:  {settings.db_path}")
    log.info(
        "QuotaLens %s on http://%s:%d  db=%s  interval=%ds",
        __version__,
        settings.host,
        settings.port,
        settings.db_path,
        settings.poll_interval_s,
    )
    try:
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
    finally:
        store.close()
        service.clear_runtime(args.data_dir, settings.profile)
        service.release_pid_file(pidfile)
    return 0


# -- background service ---------------------------------------------------------


def _serve_args(args: argparse.Namespace) -> list[str]:
    out: list[str] = []
    if getattr(args, "interval", None):
        out += ["--interval", str(args.interval)]
    if getattr(args, "port", None):
        out += ["--port", str(args.port)]
    return out


def cmd_start(args: argparse.Namespace, settings: Settings, secrets: SecretStore) -> int:
    try:
        result = service.start(args.data_dir, _serve_args(args), profile=settings.profile)
    except service.ServiceError as exc:
        print(f"not started: {exc}", file=sys.stderr)
        return 1
    port = args.port or settings.port
    print(f"started pid {result.pid}{_profile_note(settings)}")
    print(f"dashboard: http://{settings.host}:{port}/")
    print(f"pid file:  {result.pidfile}")
    print(f"log:       {result.log}")
    if not result.ready:
        # Alive but silent. Not a failure yet, and not something to claim as success.
        print(
            f"note: it has not logged anything after {service.START_TIMEOUT_S:.0f}s. "
            "It is still running; check `quotalens logs -f`.",
            file=sys.stderr,
        )
    return 0


def cmd_stop(args: argparse.Namespace, settings: Settings, secrets: SecretStore) -> int:
    installed = service.service_installed(sys.platform, Path.home(), args.data_dir)
    if installed is not None and sys.platform == "darwin" and not args.force:
        print(
            f"managed by launchd ({installed}), which restarts it on exit. Use "
            "`quotalens service uninstall`, or --force to stop it anyway.",
            file=sys.stderr,
        )
        return 1
    pid = service.stop(args.data_dir, profile=settings.profile)
    print(f"stopped pid {pid}" if pid else "not running")
    return 0


def cmd_restart(args: argparse.Namespace, settings: Settings, secrets: SecretStore) -> int:
    runtime = service.read_runtime(args.data_dir, settings.profile) or {}
    args.port = args.port or runtime.get("port")  # keep what the instance was started with
    args.interval = args.interval or runtime.get("interval_s")
    args.force = True
    cmd_stop(args, settings, secrets)
    return cmd_start(args, settings, secrets)


def cmd_status(args: argparse.Namespace, settings: Settings, secrets: SecretStore) -> int:
    report = service.status(
        args.data_dir, args.port, profile=settings.profile
    )  # None: the instance's recorded port
    print("\n".join(report.lines))
    if report.port:
        print(f"dashboard: http://{settings.host}:{report.port}/{_profile_note(settings)}")
    for line in service.service_status(sys.platform, Path.home(), args.data_dir):
        print(line)
    return report.exit_code


def cmd_logs(args: argparse.Namespace, settings: Settings, secrets: SecretStore) -> int:
    path = service.log_path(args.data_dir, settings.profile)
    if not path.exists():
        print(f"no log yet at {path}", file=sys.stderr)
        return 1
    for line in service.tail(path, args.lines):
        print(line)
    if args.follow:
        try:
            service.follow(path, print)
        except KeyboardInterrupt:
            return 0
    return 0


def cmd_service(args: argparse.Namespace, settings: Settings, secrets: SecretStore) -> int:
    home = Path.home()
    try:
        if args.action == "install":
            interval = args.interval or settings.poll_interval_s
            actions = service.install_service(
                sys.platform, home, args.data_dir, interval, profile=settings.profile
            )
        elif args.action == "uninstall":
            actions = service.uninstall_service(sys.platform, home, args.data_dir)
        else:
            for line in service.service_status(sys.platform, home, args.data_dir):
                print(line)
            return 0
    except service.ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for path in actions.written:
        print(f"wrote:   {path}")
    for path in actions.removed:
        print(f"removed: {path}")
    for cmd in actions.commands:
        print(f"ran:     {cmd}")
    for note in actions.notes:
        print(f"note:    {note}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quotalens", description="QuotaLens: local monitor for Claude subscription quota."
    )
    parser.add_argument("--version", action="version", version=f"QuotaLens {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--data-dir", type=Path, default=None, help="where the pid file and log live"
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="a second account: its own keyring entry, database, port and pid file "
        "(env: QUOTALENS_PROFILE)",
    )
    parser.add_argument(
        "--user-agent",
        help="override the User-Agent (default: the impersonated browser's; "
        "env: QUOTALENS_USER_AGENT)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="store the claude.ai session cookie in the OS keyring")
    auth.add_argument(
        "--force", action="store_true", help="store the cookie even if verification fails"
    )
    probe = sub.add_parser("probe", help="fetch once and print raw + parsed output")
    probe.add_argument(
        "--no-redact", action="store_true", help="do not mask UUID-shaped values in the output"
    )
    serve = sub.add_parser("serve", help="run the poller and the local API")
    serve.add_argument("--port", type=int, help="loopback port (default 8787)")
    serve.add_argument(
        "--interval",
        type=int,
        help=f"poll interval in seconds (default 60, min {MIN_POLL_INTERVAL_S})",
    )
    serve.add_argument("--db", type=Path, help="SQLite file path")
    serve.add_argument("--lookback", type=int, help="default burn-rate lookback in minutes")
    serve.add_argument(
        "--burn-alert", type=float, help="burn rate (pts/hr) at which the session is elevated"
    )
    serve.add_argument("--log-file", type=Path, help="also log to this file, rotated by size")

    start = sub.add_parser("start", help="start the server in the background")
    start.add_argument("--port", type=int)
    start.add_argument("--interval", type=int)
    stop = sub.add_parser("stop", help="stop the background server")
    stop.add_argument("--force", action="store_true", help="stop even if launchd manages it")
    restart = sub.add_parser("restart", help="stop, then start")
    restart.add_argument("--port", type=int)
    restart.add_argument("--interval", type=int)
    st = sub.add_parser("status", help="running? exit 0 healthy, 1 not running, 2 stalled")
    st.add_argument("--port", type=int)
    prune = sub.add_parser("prune", help="bound the raw sample table")
    prune.add_argument("--keep", type=int, help=f"samples to keep (default {DEFAULT_SAMPLE_KEEP})")
    prune.add_argument("--dry-run", action="store_true", help="report without deleting")
    prune.add_argument("--db", type=Path, help="SQLite file path")
    forget = sub.add_parser(
        "forget",
        help="remove session windows written by another collector (list them with no arguments)",
    )
    forget.add_argument("session", type=int, nargs="*", help="session window ids, as listed")
    forget.add_argument("--dry-run", action="store_true", help="report without deleting")
    forget.add_argument("--db", type=Path, help="SQLite file path")
    logs = sub.add_parser("logs", help="show the log")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("-n", "--lines", type=int, default=50)
    svc = sub.add_parser(
        "service",
        help="start automatically at login (LaunchAgent, systemd user unit, or scheduled task)",
    )
    svc.add_argument(
        "action",
        choices=["install", "uninstall", "status"],
        help="install: start at every login from now on. uninstall: undo it. status: is it set up?",
    )
    svc.add_argument("--interval", type=int, help="poll interval for the installed service")
    return parser


def main(argv: Sequence[str] | None = None, secrets: SecretStore | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    scratch_dir = args.data_dir is not None
    if args.data_dir is None:
        args.data_dir = default_data_dir()
    _setup_logging(args.verbose, getattr(args, "log_file", None))
    try:
        settings = settings_from_env(args.profile).with_overrides(user_agent=args.user_agent)
        if scratch_dir and not os.environ.get("QUOTALENS_DB") and not getattr(args, "db", None):
            # A scratch data directory must never write to the real database by default.
            settings = settings.with_overrides(
                db_path=args.data_dir / default_db_path(settings.profile).name
            )
        if args.command in DB_FLAG_COMMANDS:
            settings = settings.with_overrides(db_path=getattr(args, "db", None))
        if args.command == "serve":
            settings = validate(
                settings.with_overrides(
                    port=args.port,
                    poll_interval_s=args.interval,
                    db_path=args.db,
                    burn_lookback_min=args.lookback,
                    burn_alert_pts_per_hour=args.burn_alert,
                )
            )
    except SettingsError as exc:
        parser.error(str(exc))
    if secrets is None:
        secrets = KeyringSecretStore(profile=settings.profile)
    handlers = {
        "auth": cmd_auth,
        "probe": cmd_probe,
        "serve": cmd_serve,
        "prune": cmd_prune,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
        "logs": cmd_logs,
        "service": cmd_service,
        "forget": cmd_forget,
    }
    try:
        return handlers[args.command](args, settings, secrets)
    except SecretStoreError as exc:
        print(f"keyring error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
