"""HTTP client for fetching assets from the Layer 1 Ingest API (/artifact).

L1 stores binaries (images, PDFs, screenshots) content-addressed under
``data/storage/`` and exposes them via ``GET /artifact?path=s3://mallory-raw/...``.
This module proxies that endpoint so L2 can serve asset bytes to L3 clients
without needing direct filesystem or S3 access to the crawler machine.
"""

from __future__ import annotations

import httpx

from ..config import get_settings


def fetch_asset(storage_path: str) -> bytes:
    """Fetch an asset blob from the crawler's artifact endpoint.

    Args:
        storage_path: The ``s3://mallory-raw/<kind>/<sha>.<ext>`` URI stored
                      in the document's images / attachments / screenshot fields.

    Returns:
        Raw bytes of the asset.

    Raises:
        httpx.HTTPStatusError: If the crawler returns non-2xx (e.g. 404).
        httpx.RequestError: If the crawler is unreachable.
    """
    settings = get_settings()
    with httpx.Client(timeout=30.0) as client:
        r = client.get(
            f"{settings.crawler_ingest_url}/artifact",
            params={"path": storage_path},
        )
        r.raise_for_status()
        return r.content
