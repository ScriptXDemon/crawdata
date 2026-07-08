"""Orchestrator configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    # Control-plane store (SQLite by default; swap to Postgres via DATABASE_URL for scale).
    database_url: str = os.environ.get("DATABASE_URL", "sqlite:///./orchestrator.db")
    # Downstream services.
    crawler_api: str = os.environ.get("CRAWLER_API", "http://localhost:8099")
    l2_ingest_api: str = os.environ.get("L2_INGEST_API", "http://localhost:8000")
    # Bundled static seed (watchlist + source registry).
    seed_dir: str = os.environ.get("SEED_DIR", "./seed_data")
    cors_origins: str = os.environ.get("CORS_ORIGINS", "*")


def get_settings() -> Settings:
    return Settings()
