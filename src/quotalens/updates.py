"""Whether a newer QuotaLens has been published, asked of PyPI and nobody else.

**It only reports.** Nothing here installs, downloads or restarts anything; the
About page shows the upgrade command and the user runs it.

The request is one plain GET to PyPI's JSON for this package, through
:func:`quotalens.status.fetch` (``curl_cffi``, already a dependency, with its own
CA bundle). The only thing it carries is ``User-Agent: quotalens/<version>
(+https://quotalens.com)``: no query string, no cookie, no org id, no profile
name. The page never makes this request; the Python process does.

**Cadence.** At most once per 24 hours, remembered in the database so a restart
does not re-ask. A manual check bypasses that but not :data:`MANUAL_MIN_INTERVAL_S`.
A failure is stored, never raised, and is retried at the next 24 hour slot: a
package index being unreachable is not an alarm.

**Versions** are compared as tuples of integers from the release segments only.
Anything with a pre-release or dev suffix is ignored, so ``packaging`` (not a
runtime dependency) is not needed.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Protocol

from quotalens import __version__, status

log = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/quotalens/json"
CHECK_INTERVAL_S = 86400
MANUAL_MIN_INTERVAL_S = 60
FIRST_CHECK_DELAY_S = 300  # after start, so the first poll is never the thing it waits behind
FETCH_TIMEOUT_S = 10.0
EVENT_KIND = "update_available"
ENV_OPT_OUT = "QUOTALENS_NO_UPDATE_CHECK"
USER_AGENT_SITE = "https://quotalens.com"

_RELEASE = re.compile(r"\d+(\.\d+)*")


class UpdateStore(Protocol):
    """What :func:`check` needs from the database; ``Store`` satisfies it."""

    def read_update_check(self) -> Any: ...

    def write_update_check(
        self, checked_ts: int, latest: str | None, error: str | None
    ) -> None: ...

    def has_event(self, kind: str, detail: str) -> bool: ...

    def record_event(self, kind: str, detail: str, ts: int | None = None) -> None: ...


@dataclass(frozen=True)
class UpdateState:
    checked_ts: int | None
    latest: str | None
    current: str
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.latest is not None and is_newer(self.latest, self.current)


def parse_release(version: str) -> tuple[int, ...] | None:
    """``"2.1.0"`` to ``(2, 1, 0)``; ``None`` for a pre-release, dev build or junk."""
    text = version.strip() if isinstance(version, str) else ""
    if not _RELEASE.fullmatch(text):
        return None
    return tuple(int(part) for part in text.split("."))


def is_newer(latest: str, current: str) -> bool:
    new, old = parse_release(latest), parse_release(current)
    if new is None or old is None:
        return False
    width = max(len(new), len(old))  # 2.0 and 2.0.0 are the same release
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


def opted_out() -> bool:
    """The environment variable, which wins over the setting."""
    return os.environ.get(ENV_OPT_OUT, "").strip().lower() in {"1", "true", "yes", "on"}


def enabled(setting: bool) -> bool:
    return setting and not opted_out()


def user_agent(current: str = __version__) -> str:
    return f"quotalens/{current} (+{USER_AGENT_SITE})"


def stored_state(store: UpdateStore, current: str = __version__) -> UpdateState:
    row = store.read_update_check()
    if row is None:
        return UpdateState(None, None, current)
    return UpdateState(row.checked_ts, row.latest, current, row.error)


def due(store: UpdateStore, now: int) -> bool:
    row = store.read_update_check()
    return row is None or row.checked_ts is None or now - row.checked_ts >= CHECK_INTERVAL_S


def manual_wait(store: UpdateStore, now: int) -> int:
    """Seconds until a manual check is allowed again; 0 when it is allowed now."""
    row = store.read_update_check()
    if row is None or row.checked_ts is None:
        return 0
    return max(0, MANUAL_MIN_INTERVAL_S - (now - row.checked_ts))


def check(
    store: UpdateStore, now: int, force: bool = False, current: str = __version__
) -> UpdateState:
    """Ask PyPI if it is time, record the answer, return what is now stored.

    Blocking: call it from a worker thread. Never raises. ``force`` is the manual
    button: it skips the 24 hour rule, not the 60 second one.
    """
    if force:
        if manual_wait(store, now):
            return stored_state(store, current)
    elif not due(store, now):
        return stored_state(store, current)
    previous = stored_state(store, current)
    latest, error = previous.latest, None
    try:
        payload = status.fetch(
            PYPI_URL, FETCH_TIMEOUT_S, headers={"User-Agent": user_agent(current)}
        )
        latest = _latest_from(payload)
    except Exception as exc:  # an unreachable index must never propagate anywhere
        error = str(exc) or type(exc).__name__
        log.info("update check failed: %s", error)
    store.write_update_check(now, latest, error)
    state = UpdateState(now, latest, current, error)
    _note_available(store, state, now)
    return state


def _note_available(store: UpdateStore, state: UpdateState, now: int) -> None:
    """One event per new ``latest``, not one per daily check that still sees it."""
    if not state.available:
        return
    detail = f"{state.current} -> {state.latest}"
    if not store.has_event(EVENT_KIND, detail):
        store.record_event(EVENT_KIND, detail, now)


def _latest_from(payload: object) -> str:
    info = payload.get("info") if isinstance(payload, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    if not isinstance(version, str) or parse_release(version) is None:
        raise ValueError("no stable version in the response")
    return version.strip()


def install_method(prefix: str | None = None) -> str:
    """``pipx``, ``uv tool`` or ``pip``, from where this interpreter lives."""
    where = (prefix if prefix is not None else sys.prefix).replace("\\", "/").lower()
    if "pipx" in where:
        return "pipx"
    if "uv/tools" in where:
        return "uv tool"
    return "pip"


UPGRADE_COMMANDS = {
    "pipx": "pipx upgrade quotalens",
    "uv tool": "uv tool upgrade quotalens",
    "pip": "pip install -U quotalens",
}


def upgrade_command(prefix: str | None = None) -> str:
    return UPGRADE_COMMANDS[install_method(prefix)]
