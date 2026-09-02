"""Command line: ``quotawatch auth | probe | serve``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from quotawatch import __version__
from quotawatch.client import ClaudeClient, ClientError, has_session_key
from quotawatch.config import (
    MIN_POLL_INTERVAL_S,
    Settings,
    SettingsError,
    settings_from_env,
    validate,
)
from quotawatch.parse import ParseError, parse_overage, parse_usage
from quotawatch.secrets import (
    KeyringSecretStore,
    SecretStore,
    SecretStoreError,
    global_redactor,
    install_log_redaction,
)

log = logging.getLogger("quotawatch")

PROBE_WARNING = """\
!! WARNING: the output below is your account's usage data and may include
!! organisation ids and reset timestamps. It never includes your cookie, but
!! redact anything you consider identifying before pasting it into an issue.
"""


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    install_log_redaction()


def _load_cookie(secrets: SecretStore) -> str:
    cookie = secrets.get_cookie()
    if not cookie:
        raise SystemExit("no session cookie stored; run `quotawatch auth` first")
    global_redactor().add(cookie)
    return cookie


def read_hidden_line(prompt: str, stream: IO[Any] | None = None) -> str:
    """Read one line without echo and without the terminal's line-length cap.

    ``getpass`` reads in canonical mode, where macOS drops everything past 1024
    bytes on a line, including the newline, so a pasted Cookie header hangs the
    prompt. On a POSIX tty we switch to cbreak mode and read chunks ourselves.
    Piped stdin (``pbpaste | quotawatch auth``) is read directly.
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
    ) as client:
        usage = await client.fetch_usage()
        try:
            overage = await client.fetch_overage()
        except ClientError:
            overage = None
        return usage, overage


def cmd_auth(args: argparse.Namespace, settings: Settings, secrets: SecretStore) -> int:
    if sys.stdin.isatty():
        print(
            "Paste your claude.ai session cookie (the full Cookie header value from a\n"
            "request to claude.ai/settings/usage), then press Enter. Input is hidden.\n"
            "Tip: `pbpaste | quotawatch auth` (macOS) also works."
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
        readings = parse_usage(usage)
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
    print(PROBE_WARNING)
    print("== raw /usage ==")
    print(json.dumps(usage, indent=2, sort_keys=True))
    print("\n== raw /overage_spend_limit ==")
    print(json.dumps(overage, indent=2, sort_keys=True) if overage is not None else "(unavailable)")
    print("\n== parsed ==")
    try:
        readings = parse_usage(usage)
    except ParseError as exc:
        print(f"PARSE FAILED: {exc}")
        return 4
    for r in readings:
        reset = f"resets {r.resets_at}" if r.resets_at else "no reset time"
        print(f"  {r.window:<28} {r.label:<20} {r.pct:6.1f}%  {reset}")
    parsed_overage = parse_overage(overage)
    if parsed_overage:
        print(
            f"  overage: {parsed_overage.spent_minor / 100:.2f} / "
            f"{parsed_overage.cap_minor / 100:.2f} {parsed_overage.currency}"
        )
    if any(r.window.startswith("unknown:") for r in readings):
        print("\nNOTE: parsed via generic fallback; the endpoint shape may have drifted.")
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings, secrets: SecretStore) -> int:
    import uvicorn

    from quotawatch.api import create_app
    from quotawatch.store import Store

    cookie = secrets.get_cookie()
    if cookie:
        global_redactor().add(cookie)
    else:
        log.warning(
            "no session cookie stored; the poller will idle until you run `quotawatch auth`"
        )
    store = Store(settings.db_path)
    app = create_app(settings, store, secrets)
    log.info(
        "quotawatch %s on http://%s:%d  db=%s  interval=%ds",
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
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quotawatch", description="Local monitor for Claude subscription quota."
    )
    parser.add_argument("--version", action="version", version=f"quotawatch {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--user-agent",
        help="User-Agent to send; must match the browser the cookie was copied from "
        "(env: QUOTAWATCH_USER_AGENT)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="store the claude.ai session cookie in the OS keyring")
    auth.add_argument(
        "--force", action="store_true", help="store the cookie even if verification fails"
    )
    sub.add_parser("probe", help="fetch once and print raw + parsed output")
    serve = sub.add_parser("serve", help="run the poller and the local API")
    serve.add_argument("--port", type=int, help="loopback port (default 8787)")
    serve.add_argument(
        "--interval",
        type=int,
        help=f"poll interval in seconds (default 60, min {MIN_POLL_INTERVAL_S})",
    )
    serve.add_argument("--db", type=Path, help="SQLite file path")
    serve.add_argument("--lookback", type=int, help="default burn-rate lookback in minutes")
    return parser


def main(argv: Sequence[str] | None = None, secrets: SecretStore | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        settings = settings_from_env().with_overrides(user_agent=args.user_agent)
        if args.command == "serve":
            settings = validate(
                settings.with_overrides(
                    port=args.port,
                    poll_interval_s=args.interval,
                    db_path=args.db,
                    burn_lookback_min=args.lookback,
                )
            )
    except SettingsError as exc:
        parser.error(str(exc))
    secrets = secrets or KeyringSecretStore()
    handlers = {"auth": cmd_auth, "probe": cmd_probe, "serve": cmd_serve}
    try:
        return handlers[args.command](args, settings, secrets)
    except SecretStoreError as exc:
        print(f"keyring error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
