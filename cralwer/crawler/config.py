"""Paths and global defaults for the crawler.

The seed directory is the crawler's *only* source of truth about who/what to
watch. Everything else (storage, db, fixtures, output) lives under ``data/``.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Layout --------------------------------------------------------------
# Project root = the `cralwer/` directory (this file's grandparent's parent).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Seed location. Prefer the copy bundled inside this repo (docs/seed) so the
# crawler runs standalone after a clone; fall back to the platform tree
# (../docs/seed) when running inside the full mallery/ monorepo. Override with
# CRAWLER_SEED_DIR.
_BUNDLED_SEED = PROJECT_ROOT / "docs" / "seed"
_PLATFORM_SEED = PROJECT_ROOT.parent / "docs" / "seed"
DEFAULT_SEED_DIR = _BUNDLED_SEED if _BUNDLED_SEED.exists() else _PLATFORM_SEED
SEED_DIR = Path(os.environ.get("CRAWLER_SEED_DIR", str(DEFAULT_SEED_DIR)))

DATA_DIR = Path(os.environ.get("CRAWLER_DATA_DIR", PROJECT_ROOT / "data"))
STORAGE_DIR = DATA_DIR / "storage"          # local stand-in for s3://mallory-raw/
DB_PATH = DATA_DIR / "crawl_history.sqlite"  # our crawl_pages memory (§7A)
OUTPUT_DIR = DATA_DIR / "output"             # emitted documents/records (audit)
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"

# Local object-store URI scheme we mint for stored artifacts. Mirrors the
# contract's s3://mallory-raw/... layout so Layer 2 sees familiar paths.
STORAGE_URI_PREFIX = "s3://mallory-raw"

# --- Ingest API ----------------------------------------------------------
INGEST_BASE_URL = os.environ.get("INGEST_BASE_URL", "http://127.0.0.1:9090")
INGEST_API_PREFIX = "/ingest/v1"

# --- Fetch behaviour -----------------------------------------------------
# Hard fallbacks used when a field is absent from both the job and the
# source_registry's global_capture_defaults.
FALLBACK_CAPTURE_DEFAULTS = {
    "respect_robots_txt": True,
    "user_agent": "MalloryBot/1.0 (+contact@kssl-intel.example)",
    "crawl_delay_seconds": 2,
    "max_retries": 2,
    "timeout_seconds": 30,
}

# Read DYNAMICALLY (not cached at import) so a caller that sets these env vars
# after importing config — e.g. the §8 test harness forcing offline mode — is
# still honored regardless of import order.
#   prefer_fixtures(): try a fixture before the network (reproducible offline).
#   allow_network():   permit live HTTP when no fixture matches.
def prefer_fixtures() -> bool:
    return os.environ.get("CRAWLER_PREFER_FIXTURES", "1") == "1"


def allow_network() -> bool:
    return os.environ.get("CRAWLER_ALLOW_NETWORK", "1") == "1"


def ensure_dirs() -> None:
    """Create the runtime directories (idempotent)."""
    for d in (DATA_DIR, STORAGE_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
