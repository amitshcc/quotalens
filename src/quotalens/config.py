"""Runtime settings. Environment variables (``QUOTALENS_*``) and CLI flags only.

There is deliberately no config file: the one secret this tool holds lives in the
OS keyring (see :mod:`quotalens.secrets`), and everything else is a handful of
numbers that fit on a command line.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

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
DEFAULT_BASE_URL = "https://claude.ai"
DEFAULT_HTTP_TIMEOUT_S = 20.0
# claude.ai sits behind Cloudflare bot protection that fingerprints the TLS
# handshake; plain Python clients are challenged even with a valid cookie. We use
# curl_cffi impersonating a browser, which also supplies a matching User-Agent.
DEFAULT_IMPERSONATE = "chrome"
DEFAULT_USER_AGENT: str | None = None  # None: let the impersonated browser profile decide
APP_NAME = "quotalens"


def default_data_dir(app_name: str = APP_NAME) -> Path:
    """Per-OS user data directory, without pulling in platformdirs."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) / app_name if base else Path.home() / app_name
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / app_name


def default_db_path() -> Path:
    return default_data_dir() / f"{APP_NAME}.db"


@dataclass(frozen=True)
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    poll_interval_s: int = DEFAULT_POLL_INTERVAL_S
    burn_lookback_min: int = DEFAULT_BURN_LOOKBACK_MIN
    burn_alert_pts_per_hour: float = DEFAULT_BURN_ALERT_PTS_PER_HOUR
    sample_keep: int = DEFAULT_SAMPLE_KEEP
    db_path: Path = field(default_factory=default_db_path)
    base_url: str = DEFAULT_BASE_URL
    http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S
    user_agent: str | None = DEFAULT_USER_AGENT
    impersonate: str = DEFAULT_IMPERSONATE
    poll_enabled: bool = True

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


def settings_from_env() -> Settings:
    """Build settings from the environment, falling back to the defaults above."""
    db_raw = os.environ.get(ENV_PREFIX + "DB")
    settings = Settings(
        port=_env_int("PORT", DEFAULT_PORT),
        poll_interval_s=_env_int("INTERVAL", DEFAULT_POLL_INTERVAL_S),
        burn_lookback_min=_env_int("LOOKBACK_MINUTES", DEFAULT_BURN_LOOKBACK_MIN),
        burn_alert_pts_per_hour=_env_float("BURN_ALERT", DEFAULT_BURN_ALERT_PTS_PER_HOUR),
        sample_keep=_env_int("SAMPLE_KEEP", DEFAULT_SAMPLE_KEEP),
        db_path=Path(db_raw).expanduser() if db_raw else default_db_path(),
        base_url=os.environ.get(ENV_PREFIX + "BASE_URL", DEFAULT_BASE_URL),
        user_agent=os.environ.get(ENV_PREFIX + "USER_AGENT") or DEFAULT_USER_AGENT,
        impersonate=os.environ.get(ENV_PREFIX + "IMPERSONATE") or DEFAULT_IMPERSONATE,
    )
    return validate(settings)


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
    return settings
