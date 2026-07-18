"""FastAPI backend for the ClipForge UI.

Thin API layer only: every route calls existing pipeline/library modules;
no clip logic lives here. Static frontend served from web/.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import ROOT
from logutil import get_logger

log = get_logger("server")

_UI_LOG = ROOT / "cache" / "logs" / "ui.log"


def create_app() -> FastAPI:
    # full tracebacks land here; the API returns one-sentence messages
    try:
        _UI_LOG.parent.mkdir(parents=True, exist_ok=True)
        from logutil import add_file_handler
        add_file_handler(_UI_LOG)
    except OSError as e:
        log.warning("could not open UI log file: %s", e)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from server import jobs
        # capture the loop so pipeline worker threads can wake WS subscribers
        jobs.set_loop(asyncio.get_running_loop())
        import updater
        updater.check_async()
        import analytics
        analytics.start_background_refresh()
        import channels
        channels.start_background_poll()
        yield

    app = FastAPI(title="ClipForge", lifespan=lifespan)

    # Local, self-updating app: never let the browser serve a stale UI after an
    # update (the auto-opened window otherwise keeps cached HTML/JS/CSS and the
    # non-technical user has no reason to know about "hard refresh"). Force a
    # revalidate on every page/asset — on localhost the cost is negligible, and
    # StaticFiles still answers unchanged files with a cheap 304.
    @app.middleware("http")
    async def _no_stale_ui(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    @app.middleware("http")
    async def _workspace_scope(request, call_next):
        import config
        ws = request.headers.get("x-workspace") or "default"
        token = config.current_workspace.set(ws)
        try:
            return await call_next(request)
        finally:
            config.current_workspace.reset(token)

    from server import (routes_analytics, routes_channels,
                        routes_edit, routes_library, routes_presets,
                        routes_run, routes_settings, routes_upload,
                        routes_workspaces)
    app.include_router(routes_run.router)
    app.include_router(routes_presets.router)
    app.include_router(routes_channels.router)
    app.include_router(routes_workspaces.router)
    app.include_router(routes_library.router)
    app.include_router(routes_edit.router)
    app.include_router(routes_upload.router)
    app.include_router(routes_analytics.router)
    app.include_router(routes_settings.router)

    web_dir = ROOT / "web"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True),
                  name="web")
    else:  # API-only mode (Phase 1: frontend not built yet)
        log.warning("web/ not found — serving API only")
    return app
