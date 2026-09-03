"""FastAPI application: read-only JSON routes plus the poller on the lifespan."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from quotalens import __version__
from quotalens.burn import burn_rate
from quotalens.config import Settings
from quotalens.dashboard import as_json, build_dashboard, display_label
from quotalens.export import (
    RAW_WARNING,
    ExportError,
    csv_stream,
    filename,
    json_stream,
    resolve,
)
from quotalens.poller import ClientFactory, Poller, spend_as_dict
from quotalens.render import render_app, render_page
from quotalens.secrets import Redactor, SecretStore, global_redactor
from quotalens.sessions import rebuild as rebuild_sessions
from quotalens.state import collector_state
from quotalens.store import Store
from quotalens.views import ViewOptions, parse_view

log = logging.getLogger(__name__)

MAX_SERIES_HOURS = 24 * 90
MAX_LOOKBACK_MIN = 24 * 60
STATIC_FILES = {
    "tokens.css": "text/css; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "chart.js": "text/javascript; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}


def _static_bytes(name: str) -> bytes:
    return resources.files("quotalens.web").joinpath(name).read_bytes()


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
        rebuild_sessions(store, int(time.time()))  # backfill from every stored sample
        if settings.poll_enabled:
            poller.start()
        try:
            yield
        finally:
            await poller.stop()

    app = FastAPI(
        title="QuotaLens",
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

    def _view(request: Request) -> ViewOptions:
        return parse_view(dict(request.query_params), int(time.time()))

    def _dashboard(view: ViewOptions):
        return build_dashboard(
            settings,
            state.store,
            state.poller.status,
            int(time.time()),
            settings.burn_alert_pts_per_hour,
            view,
            cooldown_s=state.poller.cooldown_remaining(),
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request) -> HTMLResponse:
        page = render_page(_dashboard(_view(request)))
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/api/dashboard/fragment", response_class=HTMLResponse)
    def dashboard_fragment(request: Request) -> HTMLResponse:
        """The header and main content, re-rendered; the page swaps it in place."""
        fragment = render_app(_dashboard(_view(request)))
        return HTMLResponse(fragment, headers={"Cache-Control": "no-store"})

    @app.get("/api/dashboard")
    def dashboard_json(request: Request) -> dict[str, Any]:
        return as_json(_dashboard(_view(request)))

    @app.post("/api/poll")
    async def force_poll() -> dict[str, Any]:
        """Poll claude.ai now (rate limited to one forced poll per 10 seconds)."""
        before = state.poller.status.last_success_ts
        accepted, retry_in = await state.poller.force_poll()
        after = state.poller.status.last_success_ts
        return {
            "accepted": accepted,
            "retry_in_s": retry_in,
            "cooldown_s": state.poller.cooldown_remaining(),
            "state": state.poller.status.state,
            "last_success_ts": after,
            "sample_ts": after if accepted and after != before else None,
        }

    @app.post("/poll", include_in_schema=False)
    async def force_poll_form(request: Request) -> RedirectResponse:
        """No-JavaScript path: force a poll, then return to the same view."""
        await state.poller.force_poll()
        query = request.url.query
        return RedirectResponse(url=f"/?{query}" if query else "/", status_code=303)

    @app.get("/static/{name}", include_in_schema=False)
    def static(name: str) -> Response:
        if name not in STATIC_FILES:
            raise HTTPException(status_code=404)
        return Response(
            _static_bytes(name),
            media_type=STATIC_FILES[name],
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> Response:
        return Response(_static_bytes("favicon.svg"), media_type="image/svg+xml")

    @app.get("/api/events")
    def events(
        limit: int = Query(50, ge=1, le=500),
        kind: str | None = Query(None, max_length=40),
    ) -> dict[str, Any]:
        """Anomalies, threshold crossings and poll failures, newest first."""
        rows = state.store.recent_events(limit=limit, kind=kind)
        return {"events": [e.as_dict() for e in rows], "now_ts": int(time.time())}

    @app.get("/api/export.csv", include_in_schema=True)
    def export_csv(
        table: str = Query("quota", max_length=32),
        hours: float | None = Query(None, gt=0, le=MAX_SERIES_HOURS),
        raw: int = Query(0, ge=0, le=1),
    ) -> StreamingResponse:
        """Derived rows by default; raw claude.ai responses only with raw=1."""
        return _export(table, hours, raw, "csv")

    @app.get("/api/export.json", include_in_schema=True)
    def export_json(
        table: str = Query("quota", max_length=32),
        hours: float | None = Query(None, gt=0, le=MAX_SERIES_HOURS),
        raw: int = Query(0, ge=0, le=1),
    ) -> StreamingResponse:
        return _export(table, hours, raw, "json")

    def _export(table: str, hours: float | None, raw: int, fmt: str) -> StreamingResponse:
        try:
            spec = resolve(table, raw_allowed=bool(raw))
        except ExportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        since = int(time.time() - hours * 3600) if hours else None
        stream = csv_stream if fmt == "csv" else json_stream
        media = "text/csv; charset=utf-8" if fmt == "csv" else "application/json"
        name = filename(spec, fmt, settings.profile)
        headers = {"Content-Disposition": f'attachment; filename="{name}"'}
        if spec.raw:
            headers["X-QuotaLens-Warning"] = RAW_WARNING
        return StreamingResponse(
            stream(state.store, spec, since), media_type=media, headers=headers
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        poller_status = state.poller.status
        counts = state.store.counts()
        events = [e.as_dict() for e in state.store.recent_events(limit=10)]
        never_polled = poller_status.state == "starting" and not counts["quota"]
        overall = "never_polled" if never_polled else poller_status.state
        now = int(time.time())
        collector = collector_state(poller_status, settings.poll_interval_s, now)
        return {
            "status": overall,
            "version": __version__,
            "now_ts": now,
            "started_ts": poller_status.started_ts,
            "uptime_s": now - poller_status.started_ts,
            "collector": {
                "kind": collector.kind,
                "title": collector.title,
                "message": collector.message,
            },
            "poller": poller_status.as_dict(),
            "poll_interval_s": settings.poll_interval_s,
            "store": {"db_path": str(state.store.path), "rows": counts},
            "diagnostics": poller_status.diagnostics(),
            "recent_events": events,
            "note": (
                "Uses undocumented claude.ai endpoints; a parse_failed or shape_drift "
                "event means the response shape changed. Run `quotalens probe`."
            ),
        }

    @app.get("/api/quota/current")
    def quota_current() -> dict[str, Any]:
        rows = state.store.latest_quota()
        return {
            "readings": [
                {**r.as_dict(), "display": display_label(r.window, r.label)} for r in rows
            ],
            "overage": state.store.latest_overage(),
            "spend": spend_as_dict(state.poller.status.spend),
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
