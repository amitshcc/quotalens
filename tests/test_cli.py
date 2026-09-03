from __future__ import annotations

import io
import os
import pty
import sys
import threading
import time

import pytest

from conftest import COOKIE, USAGE_DOCUMENTED
from quotalens import cli
from quotalens.client import AuthError
from quotalens.secrets import MemorySecretStore

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
    assert "extra usage: $2.50 / $10.00  25%" in out
    assert SECRET not in out


def test_probe_without_cookie_exits(capsys) -> None:
    with pytest.raises(SystemExit, match="quotalens auth"):
        cli.main(["probe"], secrets=MemorySecretStore(None))


def test_serve_rejects_interval_below_floor(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["serve", "--interval", "5"], secrets=MemorySecretStore(None))
    assert "at least 30s" in capsys.readouterr().err


def test_settings_env_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QUOTALENS_PORT", "9999")
    monkeypatch.setenv("QUOTALENS_DB", str(tmp_path / "x.db"))
    settings = cli.settings_from_env()
    assert settings.port == 9999 and settings.db_path == tmp_path / "x.db"
    monkeypatch.setenv("QUOTALENS_INTERVAL", "abc")
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


def test_auth_force_stores_despite_failure(monkeypatch, capsys) -> None:
    secrets = MemorySecretStore(None)
    monkeypatch.setattr(cli, "read_hidden_line", lambda prompt: COOKIE)

    async def fake_verify(cookie: str, settings):
        raise AuthError("rejected", 403)

    monkeypatch.setattr(cli, "_verify", fake_verify)
    assert cli.main(["auth", "--force"], secrets=secrets) == 0
    assert secrets.get_cookie() == COOKIE


def test_user_agent_flag_and_env(monkeypatch) -> None:
    monkeypatch.setenv("QUOTALENS_USER_AGENT", "EnvUA/2")
    assert cli.settings_from_env().user_agent == "EnvUA/2"
    captured = {}

    async def fake_verify(cookie: str, settings):
        captured["ua"] = settings.user_agent
        return USAGE_DOCUMENTED, None

    monkeypatch.setattr(cli, "_verify", fake_verify)
    assert cli.main(["--user-agent", "FlagUA/3", "probe"], secrets=MemorySecretStore(COOKIE)) == 0
    assert captured["ua"] == "FlagUA/3"


def test_probe_masks_uuids_unless_no_redact(monkeypatch, capsys) -> None:
    payload = dict(USAGE_DOCUMENTED, organization_uuid="123e4567-e89b-12d3-a456-426614174000")

    async def fake_verify(cookie: str, settings):
        return payload, None

    monkeypatch.setattr(cli, "_verify", fake_verify)
    assert cli.main(["probe"], secrets=MemorySecretStore(COOKIE)) == 0
    out = capsys.readouterr().out
    assert "123e4567-e89b-12d3-a456-426614174000" not in out
    assert "<uuid>" in out
    assert cli.main(["probe", "--no-redact"], secrets=MemorySecretStore(COOKIE)) == 0
    assert "123e4567-e89b-12d3-a456-426614174000" in capsys.readouterr().out


def test_start_passes_data_dir_to_child(monkeypatch, tmp_path, capsys) -> None:
    from quotalens import service

    seen = {}

    def fake_start(data_dir, serve_args, **kw):
        seen["data_dir"], seen["args"] = data_dir, list(serve_args)
        return service.StartResult(4242, data_dir / "q.log", data_dir / "q.pid")

    monkeypatch.setattr(service, "start", fake_start)
    assert (
        cli.main(
            ["--data-dir", str(tmp_path), "start", "--port", "8790"],
            secrets=MemorySecretStore(COOKIE),
        )
        == 0
    )
    assert seen["data_dir"] == tmp_path
    assert "8790" in seen["args"]
    assert "http://127.0.0.1:8790/" in capsys.readouterr().out


def test_restart_keeps_the_recorded_port_and_interval(monkeypatch, tmp_path) -> None:
    from quotalens import service

    service.write_runtime(tmp_path, 8790, 30)
    seen = {}
    monkeypatch.setattr(service, "stop", lambda data_dir, **kw: None)

    def fake_start(data_dir, serve_args, **kw):
        seen["args"] = list(serve_args)
        return service.StartResult(1, data_dir / "q.log", data_dir / "q.pid")

    monkeypatch.setattr(service, "start", fake_start)
    rc = cli.main(["--data-dir", str(tmp_path), "restart"], secrets=MemorySecretStore(COOKIE))
    assert rc == 0
    assert seen["args"] == ["--interval", "30", "--port", "8790"]


def test_scratch_data_dir_implies_a_scratch_database(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("QUOTALENS_DB", raising=False)
    seen = {}

    def fake_serve(args, settings, secrets):
        seen["db"] = settings.db_path
        return 0

    monkeypatch.setattr(cli, "cmd_serve", fake_serve)
    cli.main(["--data-dir", str(tmp_path), "serve"], secrets=MemorySecretStore(COOKIE))
    assert seen["db"] == tmp_path / "quotalens.db"
    argv = ["--data-dir", str(tmp_path), "serve", "--db", str(tmp_path / "x.db")]
    cli.main(argv, secrets=MemorySecretStore(COOKIE))
    assert seen["db"] == tmp_path / "x.db"
