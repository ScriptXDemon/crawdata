"""FastAPI app — admin API + the single-page admin console."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from . import __version__
from .api import router
from .config import get_settings


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Mallory — Acquisition Orchestrator", version=__version__)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in s.cors_origins.split(",")],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "mallory-orchestrator", "version": __version__}

    _ui = Path(__file__).parent / "static" / "index.html"

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _ui.read_text() if _ui.exists() else "<h1>admin UI missing</h1>"

    return app


app = create_app()
