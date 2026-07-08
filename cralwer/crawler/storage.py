"""Object store for crawler artifacts — MinIO/S3 when configured, else local disk.

Artifacts (images, PDFs, screenshots) are addressed by an
``s3://mallory-raw/<kind>/<sha>.<ext>`` URI, mirroring the contract's storage_path
shape so Layer 2 sees familiar paths. When ``MINIO_ENDPOINT`` is set the bytes go to
MinIO; otherwise they land under ``data/storage/``. The URI is identical either way, so
nothing downstream changes.
"""
from __future__ import annotations

import hashlib
import io
import re

from . import config

# kind -> subdir (mirrors the contract examples: img/, doc/, shot/)
_KIND_DIR = {"img": "img", "doc": "doc", "shot": "shot"}

_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
         "webp": "image/webp", "pdf": "application/pdf"}

_minio_client = None


def _client():
    """Lazily build (and cache) the MinIO client; ensure the bucket exists once."""
    global _minio_client
    if _minio_client is None:
        from minio import Minio
        _minio_client = Minio(
            config.MINIO_ENDPOINT, access_key=config.MINIO_ACCESS_KEY,
            secret_key=config.MINIO_SECRET_KEY, secure=config.MINIO_SECURE,
        )
        if not _minio_client.bucket_exists(config.MINIO_BUCKET):
            _minio_client.make_bucket(config.MINIO_BUCKET)
    return _minio_client


def _safe_ext(ext: str) -> str:
    """Filesystem-safe extension: live URLs yield things like 'jpg?' or 'png#x'."""
    cleaned = re.sub(r"[^a-z0-9]", "", ext.lstrip(".").lower())[:5]
    return cleaned or "bin"


def put(data: bytes, kind: str, ext: str) -> str:
    """Store bytes content-addressably; return the s3://... storage_path URI.

    Writes to MinIO when MINIO_ENDPOINT is set, else to local disk. URI unchanged.
    """
    sub = _KIND_DIR.get(kind, kind)
    sha = hashlib.sha256(data).hexdigest()[:32]
    rel = f"{sub}/{sha}.{_safe_ext(ext)}"

    if config.MINIO_ENDPOINT:
        try:
            client = _client()
            client.put_object(
                config.MINIO_BUCKET, rel, io.BytesIO(data), length=len(data),
                content_type=_MIME.get(_safe_ext(ext), "application/octet-stream"),
            )
            return f"{config.STORAGE_URI_PREFIX}/{rel}"
        except Exception:
            pass  # fall through to local disk so a MinIO blip never drops a blob

    config.ensure_dirs()
    dest = config.STORAGE_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return f"{config.STORAGE_URI_PREFIX}/{rel}"


def local_path(storage_uri: str):
    """Resolve an s3://mallory-raw/... URI back to its local file (for tests)."""
    rel = storage_uri.replace(config.STORAGE_URI_PREFIX + "/", "")
    return config.STORAGE_DIR / rel
