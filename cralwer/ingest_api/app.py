"""Stub Ingest API — FastAPI app exposing POST /ingest/v1/page.

Each POST body is a bundle: ``{"document": {...}}`` — one raw harvested page
per bundle, no separate typed "record" (deep classification is Layer 2's
job). Accepted bundles are kept in an in-memory store and written to
``data/output/ingested.ndjson`` for audit; rejected ones return
``422 {failing_rule}``.

Run: ``uvicorn ingest_api.app:app --port 9090``  (the test harness starts it
in-process).
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from crawler import config, storage

from . import dashboard
from .validation import validate_page

# Dashboard lives at "/"; Swagger moved to /v1/docs so it doesn't clash.
app = FastAPI(title="Mallory Ingest API (stub)", version="0.1.0",
              docs_url="/v1/docs", redoc_url=None)

# In-memory acceptance ledger (cleared on restart).
ACCEPTED: list[dict] = []
REJECTED: list[dict] = []
ACCEPT_BY_SOURCE: Counter = Counter()
TOTAL_BY_SOURCE: Counter = Counter()


class IngestBundle(BaseModel):
    document: dict


def _append_ndjson(obj: dict) -> None:
    config.ensure_dirs()
    path = config.OUTPUT_DIR / "ingested.ndjson"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    """Browsable dashboard of ingested page bundles (reads the audit ndjson)."""
    return dashboard.render()


@app.get("/artifact")
def artifact(path: str):
    """Serve a stored artifact (screenshot / image / PDF) by its s3://... URI.
    Guards against path traversal — only files under the storage dir are served.
    PDFs/images open inline in the browser (Content-Disposition: inline)."""
    local = storage.local_path(path).resolve()
    root = config.STORAGE_DIR.resolve()
    if root not in local.parents or not local.exists():
        return JSONResponse(status_code=404, content={"error": "not_found"})
    # inline so PDFs/images render in the tab instead of downloading
    return FileResponse(local, headers={"Content-Disposition": "inline"})


@app.get("/raw-html")
def raw_html(doc_id: str):
    """Serve the raw source HTML of an ingested page. The HTML rides inline on
    the document (not stored as a file), so we look it up in the audit ndjson by
    document_id. Rendered in a sandboxed iframe on the dashboard, or opened raw
    in a new tab. text/plain so the browser shows the source, not a live render
    of a third-party page inside our origin."""
    for rec in reversed(dashboard.load_records()):
        doc = rec.get("document", {})
        if doc.get("document_id") == doc_id:
            html = doc.get("html") or ""
            if not html:
                return JSONResponse(status_code=404, content={"error": "no_html"})
            return HTMLResponse(content=html, headers={
                "Content-Type": "text/plain; charset=utf-8"})
    return JSONResponse(status_code=404, content={"error": "not_found"})


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "accepted": len(ACCEPTED), "rejected": len(REJECTED)}


@app.post(config.INGEST_API_PREFIX + "/page")
def ingest(bundle: IngestBundle):
    document = bundle.document
    src = document.get("source_id", "UNKNOWN")
    TOTAL_BY_SOURCE[src] += 1

    ok, failing_rule = validate_page(document)
    if not ok:
        REJECTED.append({"failing_rule": failing_rule, "url": document.get("url")})
        return JSONResponse(status_code=422, content={"failing_rule": failing_rule})

    ACCEPT_BY_SOURCE[src] += 1
    stored = {
        "ingested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "document": document,
    }
    ACCEPTED.append(stored)
    _append_ndjson(stored)
    return {"status": "accepted", "document_id": document.get("document_id")}


@app.get("/stats")
def stats() -> dict:
    """Accept-rate per source (L2 uses this to tune source tiers)."""
    by_source = {
        s: {"accepted": ACCEPT_BY_SOURCE[s], "total": TOTAL_BY_SOURCE[s],
            "accept_rate": round(ACCEPT_BY_SOURCE[s] / TOTAL_BY_SOURCE[s], 3)
            if TOTAL_BY_SOURCE[s] else 0.0}
        for s in TOTAL_BY_SOURCE
    }
    by_stream = Counter(a["document"].get("stream") for a in ACCEPTED)
    return {"accepted": len(ACCEPTED), "rejected": len(REJECTED),
            "by_stream": dict(by_stream), "by_source": by_source}


def reset() -> None:
    """Clear the ledger (used between test runs)."""
    ACCEPTED.clear(); REJECTED.clear()
    ACCEPT_BY_SOURCE.clear(); TOTAL_BY_SOURCE.clear()
