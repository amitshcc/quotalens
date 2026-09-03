"""Background quota poller with backoff.

:class:`Schedule` is a pure state machine (easy to test); :class:`Poller` wires it
to the client, the parser and the store and runs on the FastAPI lifespan.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from quotalens.client import (
    AuthError,
    BlockedError,
    ClaudeClient,
    ClientError,
    RateLimitedError,
)
from quotalens.config import Settings
from quotalens.parse import ParseError, SpendReading, UsageParse, parse_spend, parse_usage
from quotalens.secrets import Redactor, SecretStore, SecretStoreError
from quotalens.store import Store

log = logging.getLogger(__name__)

MAX_BACKOFF_S = 15 * 60
AUTH_RETRY_S = 10 * 60  # an expired cookie will not fix itself; check gently
RATE_LIMIT_MIN_S = 5 * 60
RATE_LIMIT_MAX_S = 30 * 60
BACKOFF_FACTOR = 2.0
FORCE_MIN_INTERVAL_S = 10  # one forced poll per this many seconds

ClientFactory = Callable[[str], ClaudeClient]


@dataclass
class Schedule:
    """Decides how long to wait before the next poll. Pure; no I/O."""

    interval_s: float
    failures: int = 0
    rate_limit_hits: int = 0

    def on_success(self) -> float:
        self.failures = 0
        self.rate_limit_hits = 0
        return self.interval_s

    def on_failure(self) -> float:
        self.failures += 1
        return min(self.interval_s * (BACKOFF_FACTOR ** (self.failures - 1)), MAX_BACKOFF_S)

    def on_auth_error(self) -> float:
        self.failures += 1
        return AUTH_RETRY_S

    def on_rate_limited(self, retry_after: float | None) -> float:
        self.rate_limit_hits += 1
        self.failures += 1
        backoff = min(
            RATE_LIMIT_MIN_S * (BACKOFF_FACTOR ** (self.rate_limit_hits - 1)), RATE_LIMIT_MAX_S
        )
        if retry_after is not None:
            backoff = max(backoff, retry_after)
        return backoff


@dataclass
class PollerStatus:
    """What ``/api/health`` reports. Every string here has passed the redactor."""

    state: str = (
        "starting"  # starting|ok|error|auth_expired|blocked|rate_limited|no_cookie|keyring_error
    )
    started_ts: int = field(default_factory=lambda: int(time.time()))
    last_attempt_ts: int | None = None
    last_success_ts: int | None = None
    last_error: str | None = None
    last_error_ts: int | None = None
    consecutive_failures: int = 0
    next_poll_ts: int | None = None
    polls_ok: int = 0
    polls_failed: int = 0
    org_resolved: bool = False
    overage_available: bool | None = None
    ignored_blocks: list[dict[str, str]] = field(default_factory=list)
    generic_fallback: bool = False
    spend: SpendReading | None = None
    last_windows: list[str] = field(default_factory=list)  # payload order of the last parse
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "last_attempt_ts": self.last_attempt_ts,
            "last_success_ts": self.last_success_ts,
            "last_error": self.last_error,
            "last_error_ts": self.last_error_ts,
            "consecutive_failures": self.consecutive_failures,
            "next_poll_ts": self.next_poll_ts,
            "polls_ok": self.polls_ok,
            "polls_failed": self.polls_failed,
            "org_resolved": self.org_resolved,
            "overage_available": self.overage_available,
            "started_ts": self.started_ts,
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "unrecognised_blocks": list(self.ignored_blocks),
            "generic_fallback": self.generic_fallback,
            "spend": spend_as_dict(self.spend),
        }


def spend_as_dict(spend: SpendReading | None) -> dict[str, Any] | None:
    if spend is None:
        return None
    return {
        "used_minor": spend.used_minor,
        "limit_minor": spend.limit_minor,
        "exponent": spend.exponent,
        "currency": spend.currency,
        "used_text": spend.used_text,
        "limit_text": spend.limit_text,
        "pct": spend.pct,
        "source": spend.source,
        "is_enabled": spend.is_enabled,
        "disabled_reason": spend.disabled_reason,
        "disabled_until": spend.disabled_until,
        "spend_limit_reached": spend.spend_limit_reached,
        "conflict": spend.conflict,
    }


class Poller:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        secrets: SecretStore,
        redactor: Redactor,
        client_factory: ClientFactory | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._store = store
        self._secrets = secrets
        self._redactor = redactor
        self._client_factory = client_factory or self._default_factory
        self._clock = clock
        self._client: ClaudeClient | None = None
        self._overage_failed_once = False
        self._last_ignored: frozenset[str] = frozenset()
        self.schedule = Schedule(interval_s=float(settings.poll_interval_s))
        self.status = PollerStatus()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._lock: asyncio.Lock | None = None  # created lazily on the running loop
        self._last_forced_ts: float | None = None

    def _default_factory(self, cookie: str) -> ClaudeClient:
        return ClaudeClient(
            cookie,
            base_url=self._settings.base_url,
            timeout_s=self._settings.http_timeout_s,
            user_agent=self._settings.user_agent,
            impersonate=self._settings.impersonate,
        )

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="quotalens-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def force_poll(self) -> tuple[bool, int]:
        """Poll now, at most once per FORCE_MIN_INTERVAL_S. Returns (accepted, retry_in_s)."""
        now = self._clock()
        if self._last_forced_ts is not None and now - self._last_forced_ts < FORCE_MIN_INTERVAL_S:
            retry_in = int(FORCE_MIN_INTERVAL_S - (now - self._last_forced_ts)) + 1
            self.status.extra["force_note"] = {
                "ts": int(now),
                "text": f"Forced poll suppressed: one per {FORCE_MIN_INTERVAL_S}s, "
                f"try again in {retry_in}s.",
            }
            return False, retry_in
        self._last_forced_ts = now
        self.status.extra.pop("force_note", None)
        await self.poll_once()
        return True, 0

    def _poll_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                delay = await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # belt and braces: poll_once already catches everything
                log.exception("poll loop error")
                delay = self.schedule.on_failure()
            self.status.next_poll_ts = int(self._clock() + delay)
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    # -- one poll -------------------------------------------------------------

    async def _client_for_current_cookie(self) -> ClaudeClient | None:
        cookie = self._secrets.get_cookie()
        if not cookie:
            return None
        self._redactor.add(cookie)
        if self._client is None or not self._client.uses_cookie(cookie):
            if self._client is not None:
                await self._client.close()
            self._client = self._client_factory(cookie)
        return self._client

    def _fail(self, state: str, kind: str, message: str, now: int) -> None:
        safe = self._redactor.redact(message)
        self.status.state = state
        self.status.last_error = safe
        self.status.last_error_ts = now
        self.status.extra["last_error_kind"] = kind
        self.status.consecutive_failures += 1
        self.status.polls_failed += 1
        self._store.record_event(kind, safe, ts=now)
        log.warning("poll failed (%s): %s", kind, safe)

    async def poll_once(self) -> float:
        """Fetch, parse, store. Returns seconds until the next attempt. Never raises.

        Serialised: the loop and a forced poll never overlap.
        """
        async with self._poll_lock():
            return await self._poll_once_unlocked()

    async def _poll_once_unlocked(self) -> float:
        now = int(self._clock())
        self.status.last_attempt_ts = now
        try:
            client = await self._client_for_current_cookie()
            if client is None:
                self._fail(
                    "no_cookie", "no_cookie", "no session cookie stored; run `quotalens auth`", now
                )
                return self.schedule.on_auth_error()
            return await self._collect(client, now)
        except SecretStoreError as exc:
            self._fail("keyring_error", "keyring_error", str(exc), now)
            return self.schedule.on_auth_error()  # it will not fix itself; check gently
        except AuthError as exc:
            self._fail("auth_expired", "auth_expired", str(exc), now)
            return self.schedule.on_auth_error()
        except BlockedError as exc:
            self._fail("blocked", "blocked", str(exc), now)
            return self.schedule.on_auth_error()  # will not clear itself; check gently
        except RateLimitedError as exc:
            self._fail("rate_limited", "rate_limited", str(exc), now)
            return self.schedule.on_rate_limited(exc.retry_after)
        except ParseError as exc:
            self._fail("error", "parse_failed", str(exc), now)
            return self.schedule.on_failure()
        except ClientError as exc:
            self._fail("error", "poll_error", str(exc), now)
            return self.schedule.on_failure()
        except Exception as exc:  # the loop must survive anything
            self._fail("error", "poll_error", f"unexpected {type(exc).__name__}: {exc}", now)
            return self.schedule.on_failure()

    async def _collect(self, client: ClaudeClient, now: int) -> float:
        """One successful-path poll; every error propagates to :meth:`poll_once`."""
        usage_raw = await client.fetch_usage()
        self.status.org_resolved = client.org_id is not None
        self._store.record_sample(now, "usage", usage_raw)  # keep raw even if parsing fails
        parsed = parse_usage(usage_raw)
        self._store.record_quota(now, parsed.readings)
        self._note_diagnostics(parsed, now)

        await self._poll_overage(client, now, usage_raw)

        self.status.state = "ok"
        self.status.last_success_ts = now
        self.status.consecutive_failures = 0
        self.status.polls_ok += 1
        log.info("poll ok: %d readings", len(parsed.readings))
        return self.schedule.on_success()

    def _note_diagnostics(self, parsed: UsageParse, now: int) -> None:
        self.status.generic_fallback = parsed.fallback_used
        self.status.last_windows = [r.window for r in parsed.readings]
        self.status.ignored_blocks = [{"key": b.key, "reason": b.reason} for b in parsed.ignored]
        if parsed.fallback_used:
            self._store.record_event(
                "shape_drift",
                "usage payload parsed via generic fallback; check `quotalens probe`",
                ts=now,
            )
        ignored = frozenset(b.key for b in parsed.ignored)
        if ignored and ignored != self._last_ignored:
            self._store.record_event(
                "unrecognised_block",
                "usage payload has blocks without resets_at, not charted: "
                + ", ".join(sorted(ignored)),
                ts=now,
            )
        self._last_ignored = ignored

    async def _poll_overage(self, client: ClaudeClient, now: int, usage_raw: Any) -> None:
        """Spend figures. The dedicated endpoint is optional; its failure never fails the poll.

        The usage payload's ``spend`` block is the primary source (it carries an
        explicit exponent); the endpoint only fills in ``disabled_until`` or serves
        as a last resort.
        """
        overage_raw: Any = None
        try:
            overage_raw = await client.fetch_overage()
        except AuthError:
            raise  # a 401 here means the same thing as on usage
        except ClientError as exc:
            if not self._overage_failed_once:
                self._store.record_event(
                    "overage_unavailable", self._redactor.redact(str(exc)), ts=now
                )
                self._overage_failed_once = True
        else:
            self._store.record_sample(now, "overage", overage_raw)
            self._overage_failed_once = False  # a later failure is news again
        spend = parse_spend(usage_raw, overage_raw)
        self.status.spend = spend
        if spend is None:
            self.status.overage_available = False
            return
        self._store.record_overage(now, spend)
        self.status.overage_available = True
