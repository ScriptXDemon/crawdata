"""Talk to the two neighbouring services: dispatch jobs to Layer 1, forward records to Layer 2."""

from __future__ import annotations

import httpx

from .config import get_settings

_s = get_settings()


def dispatch(job: dict, timeout: float = 60.0) -> dict:
    """POST one job to the crawler API and return its {summary, documents, records}."""
    with httpx.Client(timeout=timeout) as c:
        r = c.post(f"{_s.crawler_api}/v1/crawl", json=job)
        r.raise_for_status()
        return r.json()


def _doc_for(record: dict, documents: list[dict]) -> dict | None:
    if not documents:
        return None
    if len(documents) == 1:
        return documents[0]
    did = record.get("document_id")
    for d in documents:
        if did and (d.get("id") == did or d.get("document_id") == did or d.get("content_hash") == did):
            return d
    return documents[0]


def forward_to_l2(response: dict, timeout: float = 60.0) -> tuple[int, int]:
    """Forward a crawl response's records to L2 as {document, record} bundles.

    Returns (forwarded, accepted).
    """
    documents = response.get("documents", [])
    records = response.get("records", [])
    forwarded = accepted = 0
    with httpx.Client(timeout=timeout) as c:
        for rec in records:
            rtype = rec.get("record_type")
            doc = _doc_for(rec, documents)
            if not rtype or not doc:
                continue
            body = {"document": doc, "record": rec.get("record", {})}
            forwarded += 1
            try:
                resp = c.post(f"{_s.l2_ingest_api}/ingest/v1/{rtype}", json=body)
                if resp.status_code == 200 and resp.json().get("accepted"):
                    accepted += 1
            except Exception:
                pass
    return forwarded, accepted


def trigger_l2_pipeline(timeout: float = 120.0) -> dict:
    """Ask L2 to process the freshly-ingested staging rows into serving tables."""
    with httpx.Client(timeout=timeout) as c:
        r = c.post(f"{_s.l2_ingest_api}/ops/process")
        r.raise_for_status()
        return r.json()
