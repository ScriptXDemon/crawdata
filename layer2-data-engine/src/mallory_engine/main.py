"""FastAPI application factory for the Layer 2 Data Engineering service."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import assistant, dashboard, graph, ingest, ops, serving
from .config import get_settings

log = logging.getLogger("mallory.scheduler")


async def _scheduler_loop(interval_s: int) -> None:
    """Micro-batch loop: process pending staging rows every interval.

    ponytail: single in-process loop; move to APScheduler + a Postgres advisory lock
    when there are multiple cadences or multiple app instances.
    """
    from .db import SessionLocal
    from .pipeline.runner import process_pending

    while True:
        await asyncio.sleep(interval_s)
        try:
            with SessionLocal() as db:
                result = await asyncio.to_thread(process_pending, db)
                if result.signals_processed or result.tenders_processed:
                    log.info("scheduler: processed %s", result)
        except Exception:
            log.exception("scheduler: pipeline run failed (will retry next tick)")


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = get_settings()
    task: asyncio.Task | None = None
    if settings.scheduler_enabled:
        task = asyncio.create_task(_scheduler_loop(settings.scheduler_interval_s))
        log.info("scheduler enabled: every %ss", settings.scheduler_interval_s)
    yield
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Mallory — Layer 2 (Data Engineering)",
        version=__version__,
        summary="Ingestion (L1→L2) + processing + serving API (L2→L3). All 'vs KSSL' compute.",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(ingest.router)
    app.include_router(serving.router)
    app.include_router(assistant.router)
    app.include_router(ops.router)
    app.include_router(graph.router)
    app.include_router(dashboard.router)  # live dashboard + /api/v1/dashboard/data

    @app.get("/", tags=["meta"], include_in_schema=False)
    def index():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            "<h2 style='font-family:system-ui'>Mallory L2</h2>"
            "<ul style='font-family:system-ui;font-size:15px;line-height:1.9'>"
            "<li><a href='/dashboard'>Intelligence dashboard</a> (live)</li>"
            "<li><a href='/docs'>API explorer</a> (Swagger)</li>"
            "<li><a href='/ops/status'>Pipeline status</a></li></ul>"
        )

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok", "service": "mallory-engine", "version": __version__}

    return app


app = create_app()
