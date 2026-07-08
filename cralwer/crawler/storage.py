"""Local object store — a filesystem stand-in for s3://mallory-raw/.

Artifacts (images, PDFs, screenshots) are written under ``data/storage/`` and
addressed by an ``s3://mallory-raw/<kind>/<sha>.<ext>`` URI, mirroring the
contract's storage_path shape so Layer 2 sees familiar paths. Swap this for a
real S3/MinIO client in production without touching the pipeline.
"""
from __future__ import annotations

import hashlib
import re

from . import config

# kind -> subdir (mirrors the contract examples: img/, doc/, shot/)
_KIND_DIR = {"img": "img", "doc": "doc", "shot": "shot"}


def _safe_ext(ext: str) -> str:
    """Filesystem-safe extension: live URLs yield things like 'jpg?' or 'png#x'."""
    cleaned = re.sub(r"[^a-z0-9]", "", ext.lstrip(".").lower())[:5]
    return cleaned or "bin"


def put(data: bytes, kind: str, ext: str) -> str:
    """Store bytes content-addressably; return the s3://... storage_path URI."""
    config.ensure_dirs()
    sub = _KIND_DIR.get(kind, kind)
    sha = hashlib.sha256(data).hexdigest()[:32]
    rel = f"{sub}/{sha}.{_safe_ext(ext)}"
    dest = config.STORAGE_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return f"{config.STORAGE_URI_PREFIX}/{rel}"


def local_path(storage_uri: str):
    """Resolve an s3://mallory-raw/... URI back to its local file (for tests)."""
    rel = storage_uri.replace(config.STORAGE_URI_PREFIX + "/", "")
    return config.STORAGE_DIR / rel
