"""The persisted config store, and the precedence between the four layers.

The load-bearing test here is
:func:`test_every_config_key_is_actually_read_by_the_loader`. ``poll_enabled``
existed as a ``Settings`` field and was honoured by ``api.py`` for weeks, but no
loader ever read ``QUOTALENS_POLL_ENABLED``, so the env var was silently ignored
and a QA instance polled the live vendor on startup. A declared setting that no
layer can set is worse than a missing one: it reads as supported.
"""

from __future__ import annotations

import json

import pytest

from quotalens.config import (
    CONFIG_KEYS,
    ENV_PREFIX,
    Settings,
    SettingsError,
    config_path,
    load_settings,
    read_config_file,
    resolve_settings,
    write_config_file,
)

# A value that differs from every default, per kind, so "did it change?" is decidable.
PROBE = {"int": "4321", "float": "7.5", "str": "https://example.invalid/hook", "bool": "false"}


@pytest.mark.parametrize("key", CONFIG_KEYS, ids=lambda k: k.name)
def test_every_config_key_is_actually_read_by_the_loader(key, monkeypatch, tmp_path) -> None:
    """Set it in the environment and in the file; both must reach ``Settings``."""
    expected = {
        "int": 4321,
        "float": 7.5,
        "str": "https://example.invalid/hook",
        "bool": False,
    }[key.kind]

    monkeypatch.setenv(ENV_PREFIX + key.env, PROBE[key.kind])
    env_resolved = resolve_settings(data_dir=tmp_path)
    assert getattr(env_resolved.settings, key.field) == expected, f"{key.env} is not read"
    assert env_resolved.sources[key.name] == "env"

    monkeypatch.delenv(ENV_PREFIX + key.env)
    write_config_file(config_path("", tmp_path), {key.name: expected})
    file_resolved = resolve_settings(data_dir=tmp_path)
    assert getattr(file_resolved.settings, key.field) == expected, f"{key.name} is not read"
    assert file_resolved.sources[key.name] == "file"


def test_no_settings_field_is_declared_without_a_way_to_set_it() -> None:
    """Every field is settable by a config key, or by an env var this names.

    A new field lands in neither list by accident, and this fails until someone
    decides which it is. That decision is the point.
    """
    env_only = {
        "profile",  # QUOTALENS_PROFILE, and --profile
        "db_path",  # QUOTALENS_DB, and --db
        "base_url",  # QUOTALENS_BASE_URL: a test and debugging seam
        "user_agent",  # QUOTALENS_USER_AGENT, and --user-agent
        "impersonate",  # QUOTALENS_IMPERSONATE
    }
    fixed = {
        "host",  # loopback only, deliberately: there is no --host
        "provider",  # one provider, named once; not a config key
        "http_timeout_s",
    }
    covered = {k.field for k in CONFIG_KEYS} | env_only | fixed
    assert set(Settings.__dataclass_fields__) == covered


def test_precedence_is_flag_over_env_over_file_over_default(monkeypatch, tmp_path) -> None:
    write_config_file(config_path("", tmp_path), {"port": 9123})
    assert resolve_settings(data_dir=tmp_path).settings.port == 9123

    monkeypatch.setenv(ENV_PREFIX + "PORT", "9500")
    assert resolve_settings(data_dir=tmp_path).settings.port == 9500

    resolved = resolve_settings(data_dir=tmp_path, flags={"port": 9900})
    assert resolved.settings.port == 9900
    assert resolved.sources["port"] == "flag"


def test_an_empty_env_var_does_not_count_as_a_layer(monkeypatch, tmp_path) -> None:
    """``QUOTALENS_PORT=`` is an unset variable, not a request for port zero."""
    write_config_file(config_path("", tmp_path), {"port": 9123})
    monkeypatch.setenv(ENV_PREFIX + "PORT", "")
    assert resolve_settings(data_dir=tmp_path).settings.port == 9123


def test_a_bad_value_in_the_file_names_the_file_and_the_key(tmp_path) -> None:
    path = config_path("", tmp_path)
    write_config_file(path, {"interval": "sometimes"})
    with pytest.raises(SettingsError) as exc:
        resolve_settings(data_dir=tmp_path)
    assert str(path) in str(exc.value) and "interval" in str(exc.value)


def test_an_out_of_range_value_in_the_file_is_refused_not_repaired(tmp_path) -> None:
    """Silently clamping to the floor would hide that the file says something else."""
    write_config_file(config_path("", tmp_path), {"interval": 5})
    with pytest.raises(SettingsError) as exc:
        resolve_settings(data_dir=tmp_path)
    assert "at least 30s" in str(exc.value)


def test_an_unknown_key_in_the_file_is_named_not_ignored(tmp_path) -> None:
    path = config_path("", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"port": 9123, "colour": "blue"}))
    with pytest.raises(SettingsError) as exc:
        resolve_settings(data_dir=tmp_path)
    assert "colour" in str(exc.value)


def test_malformed_json_says_so(tmp_path) -> None:
    path = config_path("", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    with pytest.raises(SettingsError) as exc:
        read_config_file(path)
    assert "not valid JSON" in str(exc.value)


def test_a_missing_file_is_not_an_error(tmp_path) -> None:
    assert read_config_file(config_path("", tmp_path)) == {}
    assert load_settings(data_dir=tmp_path).port == 8787


def test_the_profile_gets_its_own_file(tmp_path) -> None:
    assert config_path("work", tmp_path).name == "config-work.json"
    assert config_path("", tmp_path).name == "config.json"


def test_writes_leave_no_partial_file_behind(tmp_path, monkeypatch) -> None:
    """A crash mid-write must not brick the next start."""
    path = config_path("", tmp_path)
    write_config_file(path, {"port": 9123})

    import quotalens.config as cfg

    def explode(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(cfg.os, "replace", explode)
    with pytest.raises(OSError, match="disk full"):
        write_config_file(path, {"port": 9999})

    assert read_config_file(path) == {"port": 9123}  # the old file, intact
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".config-")] == []


def test_the_cookie_can_never_be_a_config_key() -> None:
    """The keyring keeps the one secret. This file has to stay safe to paste."""
    names = {k.name for k in CONFIG_KEYS} | {k.field for k in CONFIG_KEYS}
    assert not {n for n in names if "cookie" in n or "session" in n or "secret" in n}
