from __future__ import annotations

import io
import os
import pty
import sys
import threading
import time

import pytest

from conftest import COOKIE, USAGE_DOCUMENTED
from quotawatch import cli
from quotawatch.client import AuthError
from quotawatch.secrets import MemorySecretStore

SECRET = "sk-ant-sid01-SECRETSECRETSECRET-abc"


def test_auth_stores_cookie_after_verification(monkeypatch, capsys) -> None:
    secrets = MemorySecretStore(None)
    monkeypatch.setattr(cli, "read_hidden_line", lambda prompt: COOKIE + "\n")

    async def fake_verify(cookie: str, settings):
        assert cookie == COOKIE
        return USAGE_DOCUMENTED, None

    monkeypatch.setattr(cli, "_verify", fake_verify)
    assert cli.main(["auth"], secrets=secrets) == 0
    assert secrets.get_cookie() == COOKIE
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert "stored" in out.out.lower()


def test_auth_rejected_cookie_is_not_stored(monkeypatch, capsys) -> None:
    secrets = MemorySecretStore(None)
    monkeypatch.setattr(cli, "read_hidden_line", lambda prompt: COOKIE)

    async def fake_verify(cookie: str, settings):
        raise AuthError(f"rejected Cookie: {cookie}", 401)

    monkeypatch.setattr(cli, "_verify", fake_verify)
    assert cli.main(["auth"], secrets=secrets) == 2
    assert secrets.get_cookie() is None
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert "NOT stored" in out.err


def test_probe_prints_warning_raw_and_parsed(monkeypatch, capsys) -> None:
    async def fake_verify(cookie: str, settings):
        return USAGE_DOCUMENTED, {
            "used_credits": 250,
            "monthly_credit_limit": 1000,
            "currency": "USD",
        }

    monkeypatch.setattr(cli, "_verify", fake_verify)
    assert cli.main(["probe"], secrets=MemorySecretStore(COOKIE)) == 0
    out = capsys.readouterr().out
    assert out.startswith("!! WARNING")
    assert '"five_hour"' in out
    assert "limit:opus" in out
    assert "overage: 2.50 / 10.00 USD" in out
    assert SECRET not in out


def test_probe_without_cookie_exits(capsys) -> None:
    with pytest.raises(SystemExit, match="quotawatch auth"):
        cli.main(["probe"], secrets=MemorySecretStore(None))


def test_serve_rejects_interval_below_floor(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["serve", "--interval", "5"], secrets=MemorySecretStore(None))
    assert "at least 30s" in capsys.readouterr().err


def test_settings_env_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QUOTAWATCH_PORT", "9999")
    monkeypatch.setenv("QUOTAWATCH_DB", str(tmp_path / "x.db"))
    settings = cli.settings_from_env()
    assert settings.port == 9999 and settings.db_path == tmp_path / "x.db"
    monkeypatch.setenv("QUOTAWATCH_INTERVAL", "abc")
    with pytest.raises(cli.SettingsError):
        cli.settings_from_env()


def test_read_hidden_line_from_pipe() -> None:
    assert cli.read_hidden_line("x", io.StringIO(COOKIE + "\r\n")) == COOKIE


@pytest.mark.skipif(sys.platform.startswith("win"), reason="pty is POSIX only")
def test_read_hidden_line_survives_paste_longer_than_tty_line_limit(capsys) -> None:
    """A real Cookie header exceeds the 1024-byte canonical line cap on macOS."""
    long_cookie = "sessionKey=sk-ant-" + "x" * 3000 + "; lastActiveOrg=abc"
    master, slave = pty.openpty()
    stream = os.fdopen(slave, "r", closefd=True)
    result: list[str] = []

    def paste() -> None:
        time.sleep(0.3)  # the user pastes after the prompt has switched modes
        os.write(master, long_cookie.encode() + b"\n")

    def read() -> None:
        with capsys.disabled():
            result.append(cli.read_hidden_line("Cookie: ", stream))

    reader = threading.Thread(target=read, daemon=True)
    threading.Thread(target=paste, daemon=True).start()
    reader.start()
    reader.join(timeout=5)
    try:
        assert not reader.is_alive(), "reader hung: newline never arrived"
        assert result == [long_cookie]
    finally:
        os.close(master)
        if not reader.is_alive():
            stream.close()
