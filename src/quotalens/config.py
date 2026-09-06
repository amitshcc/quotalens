"""Runtime settings: CLI flags, ``QUOTALENS_*`` environment variables, ``config.json``.

**This module used to say there was deliberately no config file**, on the grounds
that the one secret lives in the OS keyring and everything else is a handful of
numbers that fit on a command line. That was right while every setting was a flag
on a command you typed once. It stopped being right when settings had to survive
a restart: a port you chose, notifications you turned on, a retention period. A
flag cannot do that, so the decision is overturned here rather than worked around.

Precedence, highest wins::

    CLI flag  >  QUOTALENS_* environment variable  >  config.json  >  built-in default

A flag still wins, so no existing invocation changes meaning.

**The cookie is never in this file.** The keyring keeps the one secret, and a
config file that can be pasted into an issue has to stay safe to paste into an
issue.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

ENV_PREFIX = "QUOTALENS_"

DEFAULT_HOST = "127.0.0.1"  # loopback only; no --host flag in this milestone
DEFAULT_PORT = 8787
DEFAULT_POLL_INTERVAL_S = 60
MIN_POLL_INTERVAL_S = 30  # floor: below this we would be rate-limiting ourselves
DEFAULT_BURN_LOOKBACK_MIN = 15
DEFAULT_BURN_ALERT_PTS_PER_HOUR = 20.0  # elevated when the session window burns faster
# Raw payloads are the endpoint-drift record and also the one table that grows without
# bound. Keep roughly a week of them at a minute a poll; the first sample of every
# distinct payload shape is kept forever regardless.
DEFAULT_SAMPLE_KEEP = 20_000
PRUNE_EVERY_S = 6 * 3600
# Opt-in only. One POST per threshold crossing, no account identifier in the body.
DEFAULT_WEBHOOK_URL: str | None = None
DEFAULT_BASE_URL = "https://claude.ai"


@dataclass(frozen=True)
class Provider:
    """The one place a vendor is named.

    QuotaLens watches one provider today and will watch more. This exists so that
    adding the second is a new value here rather than a grep for "Claude" across
    the tree: every string the owner reads on screen, in the cookie prompt, or in
    a client error asks this instead of spelling the vendor out.

    Deliberately *not* a registry. There is no config key and no flag to choose a
    provider, because there is only one; internal names (``ClaudeClient``, the
    parser, the store schema) stay Claude-specific until a second one is real, and
    renaming them now would be churn with no reader.
    """

    key: str  # stable identifier, for a future config key or store column
    display_name: str  # what the owner calls it: "Claude"
    base_url: str
    usage_command: str  # the vendor's own attribution tool: "claude /usage"

    @property
    def host(self) -> str:
        """The bare host, for prose: "claude.ai", not "https://claude.ai"."""
        return self.base_url.split("://", 1)[-1].rstrip("/")


CLAUDE = Provider(
    key="claude",
    display_name="Claude",
    base_url=DEFAULT_BASE_URL,
    usage_command="claude /usage",
)
DEFAULT_HTTP_TIMEOUT_S = 20.0
# claude.ai sits behind Cloudflare bot protection that fingerprints the TLS
# handshake; plain Python clients are challenged even with a valid cookie. We use
# curl_cffi impersonating a browser, which also supplies a matching User-Agent.
DEFAULT_IMPERSONATE = "chrome"
DEFAULT_USER_AGENT: str | None = None  # None: let the impersonated browser profile decide
APP_NAME = "quotalens"
# A profile is a second account: its own keyring entry, database, port and pid file.
# Two accounts is two processes and two bookmarks, not account switching in one process.
PROFILE_PORT_BASE = 8788
PROFILE_PORT_SPAN = 100
_PROFILE_CLEAN = re.compile(r"[^a-z0-9_-]+")


def normalise_profile(name: str | None) -> str:
    """Lower-cased and safe for a filename, a port derivation and a keyring entry."""
    if not name:
        return ""
    cleaned = _PROFILE_CLEAN.sub("-", name.strip().lower()).strip("-")
    if not cleaned:
        raise SettingsError(f"profile name {name!r} has no usable characters")
    if len(cleaned) > 40:
        raise SettingsError("profile name must be 40 characters or fewer")
    return cleaned


def profile_suffix(profile: str) -> str:
    return f"-{profile}" if profile else ""


def default_port(profile: str = "") -> int:
    """8787 for the default profile; a stable derived port for a named one.

    Derived with crc32 rather than ``hash`` so it is the same port on every run.
    Two profiles can still collide, and then the bind error says so and ``--port``
    settles it.
    """
    if not profile:
        return DEFAULT_PORT
    return PROFILE_PORT_BASE + zlib.crc32(profile.encode()) % PROFILE_PORT_SPAN


def default_data_dir(app_name: str = APP_NAME) -> Path:
    """Per-OS user data directory, without pulling in platformdirs."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) / app_name if base else Path.home() / app_name
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / app_name


def default_db_path(profile: str = "") -> Path:
    return default_data_dir() / f"{APP_NAME}{profile_suffix(profile)}.db"


@dataclass(frozen=True)
class Settings:
    profile: str = ""  # "" is the default profile
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    poll_interval_s: int = DEFAULT_POLL_INTERVAL_S
    burn_lookback_min: int = DEFAULT_BURN_LOOKBACK_MIN
    burn_alert_pts_per_hour: float = DEFAULT_BURN_ALERT_PTS_PER_HOUR
    sample_keep: int = DEFAULT_SAMPLE_KEEP
    webhook_url: str | None = DEFAULT_WEBHOOK_URL
    db_path: Path = field(default_factory=default_db_path)
    base_url: str = DEFAULT_BASE_URL
    provider: Provider = CLAUDE
    http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S
    user_agent: str | None = DEFAULT_USER_AGENT
    impersonate: str = DEFAULT_IMPERSONATE
    poll_enabled: bool = True
    # None until the first run writes one. That is not a missing default: it is how
    # "this database predates the setting, so prune nothing yet" is represented.
    # See quotalens.retention.initial_retention.
    retention: str | None = None
    # Off by default, matching the webhook's opt-in posture in alerts.py: a tool
    # that pushes to your desktop without being asked has overstepped.
    notify: bool = False
    notify_thresholds: str = "50,75,90"
    # Separate from the threshold toggle and on by default: crossing 50% is
    # information, and money leaving is not the same class of event.
    notify_credits: bool = True
    # On by default: it is one small GET per vendor every five minutes and it
    # answers "is it them or me". Off must stop the requests, not hide the row.
    status_row: bool = True
    status_vendors: str = "claude,openai"

    def with_overrides(self, **kwargs: object) -> Settings:
        """Return a copy with the given non-``None`` fields replaced."""
        return replace(self, **{k: v for k, v in kwargs.items() if v is not None})


class SettingsError(ValueError):
    """A setting is missing or out of range."""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SettingsError(f"{ENV_PREFIX}{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise SettingsError(f"{ENV_PREFIX}{name} must be a number, got {raw!r}") from exc


def validate(settings: Settings) -> Settings:
    """Enforce the invariants that protect the user (and claude.ai) from us."""
    if settings.poll_interval_s < MIN_POLL_INTERVAL_S:
        raise SettingsError(
            f"poll interval must be at least {MIN_POLL_INTERVAL_S}s "
            f"(got {settings.poll_interval_s}s); polling faster invites rate limiting"
        )
    if not (1 <= settings.port <= 65535):
        raise SettingsError(f"port must be 1-65535, got {settings.port}")
    if settings.burn_lookback_min < 1:
        raise SettingsError("burn lookback must be at least 1 minute")
    if settings.burn_alert_pts_per_hour <= 0:
        raise SettingsError("burn alert threshold must be positive")
    if settings.sample_keep < 100:
        raise SettingsError("sample retention must keep at least 100 samples")
    if settings.webhook_url and not settings.webhook_url.startswith(("http://", "https://")):
        raise SettingsError("the webhook URL must be http:// or https://")
    if settings.retention is not None:
        from quotalens.retention import RETENTION_BY_KEY

        if settings.retention not in RETENTION_BY_KEY:
            raise SettingsError(
                f"retention must be one of {', '.join(RETENTION_BY_KEY)}, "
                f"got {settings.retention!r}"
            )
    return settings


def config_path(profile: str = "", data_dir: Path | None = None) -> Path:
    """``config.json`` beside the database, profile-suffixed exactly as it is."""
    return (data_dir or default_data_dir()) / f"config{profile_suffix(profile)}.json"


@dataclass(frozen=True)
class ConfigKey:
    """One setting that can be written to ``config.json``.

    The allow-list this describes is shared by ``quotalens config set`` and the
    settings panel on purpose: a CLI and a UI over one store, not two config
    systems that disagree about what a key is called.
    """

    name: str  # what the user types: "port"
    field: str  # the Settings field it fills
    env: str  # the QUOTALENS_ suffix that overrides it
    kind: str  # int | float | str | bool
    help: str
    default: Callable[[str], Any]  # takes the profile; most ignore it
    panel: bool = True  # offered in the settings panel


CONFIG_KEYS: tuple[ConfigKey, ...] = (
    # The port is the address of the page the panel is served from, so changing it
    # from that page means the form's own response never arrives. CLI only.
    ConfigKey("port", "port", "PORT", "int", "loopback port", default_port, panel=False),
    ConfigKey(
        "interval",
        "poll_interval_s",
        "INTERVAL",
        "int",
        f"seconds between polls (minimum {MIN_POLL_INTERVAL_S})",
        lambda _p: DEFAULT_POLL_INTERVAL_S,
    ),
    ConfigKey(
        "lookback",
        "burn_lookback_min",
        "LOOKBACK_MINUTES",
        "int",
        "minutes of history the burn rate is measured over",
        lambda _p: DEFAULT_BURN_LOOKBACK_MIN,
    ),
    ConfigKey(
        "burn_alert",
        "burn_alert_pts_per_hour",
        "BURN_ALERT",
        "float",
        "burn rate that raises an alert, in points per hour",
        lambda _p: DEFAULT_BURN_ALERT_PTS_PER_HOUR,
    ),
    ConfigKey(
        "sample_keep",
        "sample_keep",
        "SAMPLE_KEEP",
        "int",
        "raw payloads kept, as a row cap",
        lambda _p: DEFAULT_SAMPLE_KEEP,
    ),
    ConfigKey(
        "webhook_url",
        "webhook_url",
        "WEBHOOK_URL",
        "str",
        "where threshold crossings are POSTed (opt-in)",
        lambda _p: DEFAULT_WEBHOOK_URL,
    ),
    ConfigKey(
        "retention",
        "retention",
        "RETENTION",
        "str",
        "how long detail rows are kept: 1week, 1month, 3months, 6months, 1year",
        lambda _p: None,  # unset until the first run decides; see retention.initial_retention
    ),
    ConfigKey(
        "notify",
        "notify",
        "NOTIFY",
        "bool",
        "desktop notification when a window crosses a threshold",
        lambda _p: False,
    ),
    ConfigKey(
        "notify_thresholds",
        "notify_thresholds",
        "NOTIFY_THRESHOLDS",
        "str",
        "percentages that trigger a notification, comma separated",
        lambda _p: "50,75,90",
    ),
    ConfigKey(
        "notify_credits",
        "notify_credits",
        "NOTIFY_CREDITS",
        "bool",
        "notify when usage credits start being spent",
        lambda _p: True,
    ),
    ConfigKey(
        "status_row",
        "status_row",
        "STATUS_ROW",
        "bool",
        "show each vendor's own status page state in the side panel",
        lambda _p: True,
    ),
    ConfigKey(
        "status_vendors",
        "status_vendors",
        "STATUS_VENDORS",
        "str",
        "which vendors the status row covers, comma separated",
        lambda _p: "claude,openai",
    ),
    ConfigKey(
        "poll_enabled",
        "poll_enabled",
        "POLL_ENABLED",
        "bool",
        "poll the provider at all; off makes this a viewer for existing data",
        lambda _p: True,
        panel=False,
    ),
)

CONFIG_KEYS_BY_NAME = {k.name: k for k in CONFIG_KEYS}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def parse_config_value(key: ConfigKey, raw: object, where: str) -> Any:
    """Coerce one value, or raise ``SettingsError`` naming where it came from.

    ``where`` is the thing a reader can go and fix -- an env var name or a file
    path and key. A bad value is never silently ignored and never silently
    repaired; that is the whole reason this returns or raises and does nothing
    in between.
    """
    if key.kind == "str":
        return None if raw is None or raw == "" else str(raw)
    if key.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise SettingsError(f"{where} must be true or false, got {raw!r}")
    try:
        return int(raw) if key.kind == "int" else float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        noun = "an integer" if key.kind == "int" else "a number"
        raise SettingsError(f"{where} must be {noun}, got {raw!r}") from exc


def read_config_file(path: Path) -> dict[str, Any]:
    """The stored settings, or ``{}``. Unreadable is an error; absent is not."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{path} must contain a JSON object, got {type(data).__name__}")
    return data


def write_config_file(path: Path, data: dict[str, Any]) -> None:
    """Atomically, via a temp file in the same directory and ``os.replace``.

    The API writes this while the poller is running. A half-written config file
    that bricks the next start is not an acceptable cost for a convenience.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class Resolution:
    """Merged settings, plus where each value actually came from.

    ``sources`` is what makes ``quotalens config list`` worth having: a port that
    is not what you set is a precedence question, and this answers it without
    anyone having to read this module.
    """

    settings: Settings
    sources: dict[str, str]  # config key name -> "flag" | "env" | "file" | "default"


def resolve_settings(
    profile: str | None = None,
    *,
    data_dir: Path | None = None,
    flags: dict[str, Any] | None = None,
) -> Resolution:
    """Apply the four layers in order and record which one won each key."""
    if profile is None:
        profile = os.environ.get(ENV_PREFIX + "PROFILE", "")
    profile = normalise_profile(profile)

    path = config_path(profile, data_dir)
    stored = read_config_file(path)
    unknown = sorted(set(stored) - set(CONFIG_KEYS_BY_NAME))
    if unknown:
        raise SettingsError(
            f"{path} has {'keys' if len(unknown) > 1 else 'a key'} this version does not "
            f"know: {', '.join(unknown)}. Remove {'them' if len(unknown) > 1 else 'it'} "
            f"or run `quotalens config unset <key>`."
        )

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    flags = flags or {}
    for key in CONFIG_KEYS:
        if flags.get(key.name) is not None:
            values[key.field] = parse_config_value(key, flags[key.name], f"--{key.name}")
            sources[key.name] = "flag"
            continue
        raw_env = os.environ.get(ENV_PREFIX + key.env)
        if raw_env not in (None, ""):
            values[key.field] = parse_config_value(key, raw_env, ENV_PREFIX + key.env)
            sources[key.name] = "env"
            continue
        if key.name in stored:
            values[key.field] = parse_config_value(
                key, stored[key.name], f"{path} key {key.name!r}"
            )
            sources[key.name] = "file"
            continue
        values[key.field] = key.default(profile)
        sources[key.name] = "default"

    db_raw = os.environ.get(ENV_PREFIX + "DB")
    settings = Settings(
        profile=profile,
        db_path=Path(db_raw).expanduser() if db_raw else default_db_path(profile),
        base_url=os.environ.get(ENV_PREFIX + "BASE_URL", DEFAULT_BASE_URL),
        user_agent=os.environ.get(ENV_PREFIX + "USER_AGENT") or DEFAULT_USER_AGENT,
        impersonate=os.environ.get(ENV_PREFIX + "IMPERSONATE") or DEFAULT_IMPERSONATE,
        **values,
    )
    return Resolution(validate(settings), sources)


def load_settings(
    profile: str | None = None,
    *,
    data_dir: Path | None = None,
    flags: dict[str, Any] | None = None,
) -> Settings:
    """The merged settings. See :func:`resolve_settings` for where each came from."""
    return resolve_settings(profile, data_dir=data_dir, flags=flags).settings
