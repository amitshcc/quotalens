"""FastAPI application: read-only JSON routes plus the poller on the lifespan."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from quotawatch import __version__
from quotawatch.burn import burn_rate
from quotawatch.config import Settings
from quotawatch.poller import ClientFactory, Poller
from quotawatch.secrets import Redactor, SecretStore, global_redactor
from quotawatch.store import Store

log = logging.getLogger(__name__)

MAX_SERIES_HOURS = 24 * 90
MAX_LOOKBACK_MIN = 24 * 60


@dataclass
class AppState:
    settings: Settings
    store: Store
    poller: Poller
    redactor: Redactor


def create_app(
    settings: Settings,
    store: Store,
    secrets: SecretStore,
    *,
    redactor: Redactor | None = None,
    client_factory: ClientFactory | None = None,
) -> FastAPI:
    redactor = redactor or global_redactor()
    poller = Poller(settings, store, secrets, redactor, client_factory=client_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.poll_enabled:
            poller.start()
        try:
            yield
        finally:
            await poller.stop()

    app = FastAPI(
        title="quotawatch",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )
    state = AppState(settings, store, poller, redactor)
    app.state.qw = state

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never echo exception text to the client: it could carry a header or a URL.
        log.error("unhandled error on %s: %s", request.url.path, redactor.redact(str(exc)))
        return JSONResponse(status_code=500, content={"error": "internal error; see server log"})

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        poller_status = state.poller.status
        counts = state.store.counts()
        events = [e.as_dict() for e in state.store.recent_events(limit=10)]
        never_polled = poller_status.state == "starting" and not counts["quota"]
        overall = "never_polled" if never_polled else poller_status.state
        return {
            "status": overall,
            "version": __version__,
            "now_ts": int(time.time()),
            "poller": poller_status.as_dict(),
            "poll_interval_s": settings.poll_interval_s,
            "store": {"db_path": str(state.store.path), "rows": counts},
            "recent_events": events,
            "note": (
                "Uses undocumented claude.ai endpoints; a parse_failed or shape_drift "
                "event means the response shape changed. Run `quotawatch probe`."
            ),
        }

    @app.get("/api/quota/current")
    def quota_current() -> dict[str, Any]:
        rows = state.store.latest_quota()
        return {
            "readings": [r.as_dict() for r in rows],
            "overage": state.store.latest_overage(),
            "last_success_ts": state.poller.status.last_success_ts,
        }

    @app.get("/api/quota/series")
    def quota_series(
        hours: float = Query(1.0, gt=0, le=MAX_SERIES_HOURS),
        window: str | None = Query(None, max_length=100),
    ) -> dict[str, Any]:
        now = int(time.time())
        since = now - int(hours * 3600)
        rows = state.store.quota_series(since, window=window)
        return {
            "since_ts": since,
            "until_ts": now,
            "hours": hours,
            "window": window,
            "readings": [r.as_dict() for r in rows],
        }

    @app.get("/api/burn")
    def burn(
        window: str | None = Query(None, max_length=100),
        lookback: int = Query(settings.burn_lookback_min, ge=1, le=MAX_LOOKBACK_MIN),
    ) -> dict[str, Any]:
        now = int(time.time())
        lookback_s = lookback * 60
        windows = [window] if window else state.store.windows()
        if window and window not in state.store.windows():
            raise HTTPException(status_code=404, detail=f"unknown window {window!r}")
        results = []
        for name in windows:
            # Read a generous slice so the reset-split has context before the lookback.
            rows = state.store.quota_series(now - max(lookback_s * 4, 6 * 3600), window=name)
            results.append(burn_rate(name, rows, lookback_s, now).as_dict())
        return {"now_ts": now, "lookback_minutes": lookback, "burn": results}

    return app
