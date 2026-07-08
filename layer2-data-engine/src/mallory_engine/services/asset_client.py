"""Fetch asset bytes for a document's ``s3://mallory-raw/...`` storage_path.

Two backends: read directly from MinIO/S3 when ``minio_endpoint`` is set (the crawler
wrote the blobs there); otherwise proxy the crawler's ``GET /artifact?path=...``. The
``s3://mallory-raw/<kind>/<sha>.<ext>`` URI is the same in both cases.
"""

from __future__ import annotations

import httpx

from ..config import get_settings

_minio_client = None


def _client():
    global _minio_client
    if _minio_client is None:
        from minio import Minio
        s = get_settings()
        _minio_client = Minio(
            s.minio_endpoint, access_key=s.minio_access_key,
            secret_key=s.minio_secret_key, secure=s.minio_secure,
        )
    return _minio_client


def _object_key(storage_path: str) -> str:
    """s3://mallory-raw/img/abc.jpg -> img/abc.jpg (strip scheme + bucket)."""
    return storage_path.split("mallory-raw/", 1)[-1]


def fetch_asset(storage_path: str) -> bytes:
    """Fetch an asset blob by its ``s3://mallory-raw/<kind>/<sha>.<ext>`` URI.

    From MinIO when configured, else the crawler's /artifact proxy.

    Raises:
        Exception: on a missing object / unreachable backend (callers handle it).
    """
    settings = get_settings()
    if settings.minio_endpoint:
        resp = _client().get_object(settings.minio_bucket, _object_key(storage_path))
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    with httpx.Client(timeout=30.0) as client:
        r = client.get(f"{settings.crawler_ingest_url}/artifact", params={"path": storage_path})
        r.raise_for_status()
        return r.content
