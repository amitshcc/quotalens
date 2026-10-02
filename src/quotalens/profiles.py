"""Find the profiles in a data directory and say whether each one is running."""

from __future__ import annotations

import urllib.error
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from quotalens import service
from quotalens.config import APP_NAME, default_db_path, default_port, profile_suffix

HEALTH_TIMEOUT_S = 3.0
DEFAULT_PROFILE_NAME = "default"


@dataclass(frozen=True)
class ProfileInfo:
    profile: str  # "default" for the default profile
    port: int
    state: str  # running | stalled | stopped
    db_path: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _suffix_to_profile(stem: str, prefix: str) -> str | None:
    """Inverse of ``profile_suffix``: "quotalens-work" -> "work", "quotalens" -> ""."""
    if stem == prefix:
        return ""
    marker = f"{prefix}{profile_suffix('x')[:-1]}"  # "<prefix>-"
    if stem.startswith(marker) and len(stem) > len(marker):
        return stem[len(marker) :]
    return None


def discover_profiles(data_dir: Path) -> list[str]:
    """Profile names ("" is the default) that have a database or a config file."""
    found: set[str] = set()
    for pattern, prefix in ((f"{APP_NAME}*.db", APP_NAME), ("config*.json", "config")):
        for path in data_dir.glob(pattern):
            name = _suffix_to_profile(path.stem, prefix)
            if name is not None:
                found.add(name)
    return sorted(found, key=lambda n: (n != "", n))


def profile_info(
    data_dir: Path,
    profile: str,
    fetch: Callable[..., Any] = service.fetch_json,
) -> ProfileInfo:
    runtime = service.read_runtime(data_dir, profile) or {}
    port = int(runtime.get("port") or default_port(profile))
    state = "stopped"
    if service.running_pid(service.pid_path(data_dir, profile)):
        state = "running"
        try:
            fetch(f"http://127.0.0.1:{port}/api/health", timeout_s=HEALTH_TIMEOUT_S)
        except (urllib.error.URLError, OSError, ValueError):
            state = "stalled"
    return ProfileInfo(
        profile=profile or DEFAULT_PROFILE_NAME,
        port=port,
        state=state,
        db_path=str(data_dir / default_db_path(profile).name),
    )


def list_profiles(
    data_dir: Path, fetch: Callable[..., Any] = service.fetch_json
) -> list[ProfileInfo]:
    return [profile_info(data_dir, name, fetch) for name in discover_profiles(data_dir)]


def format_table(rows: list[ProfileInfo]) -> str:
    header = ("PROFILE", "PORT", "STATE", "DB")
    body = [(r.profile, str(r.port), r.state, r.db_path) for r in rows]
    widths = [max(len(row[i]) for row in [header, *body]) for i in range(len(header))]
    lines = [
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in [header, *body]
    ]
    return "\n".join(lines)
