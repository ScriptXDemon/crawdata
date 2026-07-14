"""Crawler HTTP API.

Endpoints
---------
POST /v1/crawl                run one crawl job (§2) -> raw page bundles (§3)
POST /v1/crawl/batch          run many jobs ({"jobs": [ ... ]}, "parallel": N)
POST /v1/check-keywords       probe one URL for keyword relevance (no crawl/ingest)
POST /v1/check-keywords/batch probe many URLs at once (filter before crawling)
POST /v1/suggest-job          build a Job with probe-adaptive auto-selected keywords
GET  /v1/schema               JSON schemas for the job input + the document output
GET  /health                  liveness

Request body for /v1/crawl is a crawl job. Add ``"forward_to_ingest": true`` to
also POST each kept page to the Ingest API (the production push flow);
otherwise documents are only returned inline (pull flow, ideal for Layer 2
testing).

Run:  python run.py crawler-api      # serves on http://127.0.0.1:8099
Auth is intentionally open for the test build (add a bearer dependency for prod).
"""
from __future__ import annotations

from collections import Counter
from threading import Lock

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from crawler import config
from crawler.async_engine import request_stop, run_batch_async, stop_requested
from crawler.jobgen import generate as generate_jobs
from crawler.keywords import get_corpus
from crawler.models import Document, Job
from crawler.seed import load_seed

from . import dashboard

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # scripts/ on path
from scripts.check_keywords import check_keywords as _check_keywords
from scripts.check_keywords import discover_keywords as _discover_keywords
from crawler.jobgen import _candidate_pool

app = FastAPI(title="Mallory Crawler API (Layer 1)", version="0.1.0",
              docs_url="/v1/docs", redoc_url=None)

_SEED = load_seed()
_KP = get_corpus()          # global keyword-corpus trie (keep-gate), built once

# Progress tracker for active batch runs
_batch_lock = Lock()
_batch_status: dict = {"running": False, "total": 0, "done": 0, "current_job": "", "results": []}


class CrawlRequest(Job):
    """A crawl job (§2) plus API-only knobs."""
    forward_to_ingest: bool = False
    l2_ingest_url: str | None = None
    wayback: bool = False          # C2 archival fallback (dashboard Wayback toggle); OFF = C1/C3 only


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "entities": len(_SEED.entities),
            "sources": len(_SEED.sources), "keywords": len(_KP)}


@app.get("/v1/camofox")
def camofox_status() -> dict:
    """C3 CamoFox stealth engine status for the dashboard: is it enabled
    (CAMOFOX_ENABLED=1) and is the server answering /health."""
    from crawler import camofox_client
    return {"enabled": camofox_client.enabled(),
            "healthy": camofox_client.health(),
            "url": camofox_client.base_url()}


@app.get("/v1/config")
def api_config() -> dict:
    """Server-side wiring the dashboard needs. l2_forward_url is the URL the CRAWLER uses to
    reach L2 (a container name like http://l2:8000 in Docker, or http://127.0.0.1:8000 native)
    — distinct from the browser-facing URL the operator types for the 'Process in L2' button."""
    import os
    return {"l2_forward_url": os.environ.get("L2_INGEST_URL", "")}


@app.get("/v1/metrics")
def metrics() -> dict:
    """Live hardware + crawl metrics for the dashboard strip. CPU/RAM are the WSL2-VM view
    (psutil reads the container's /proc) — i.e. 'is the whole box busy'. batch is coarse for
    the async pool (per-job counts land only at batch end); the CPU/RAM gauges are the live
    signal that the pool is working."""
    import os

    import psutil

    vm = psutil.virtual_memory()
    with _batch_lock:
        bs = dict(_batch_status)
    agg = {"jobs": 0, "fetched": 0, "kept": 0, "sent": 0, "accepted": 0}
    for r in bs.get("results", []) or []:
        agg["jobs"] += 1
        for k in ("fetched", "kept", "sent", "accepted"):
            agg[k] += int(r.get(k, 0) or 0)
    W = int(os.environ.get("CRAWLER_BROWSERS", "8"))
    T = int(os.environ.get("CRAWLER_TABS_PER_BROWSER", "12"))
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.3),   # accurate instantaneous read
        "cpu_count": psutil.cpu_count(),
        "ram": {"used_gb": round((vm.total - vm.available) / 1e9, 1),
                "total_gb": round(vm.total / 1e9, 1), "percent": vm.percent},
        "pool": {"engine": "async",
                 "browsers": W, "tabs_per_browser": T, "tabs": W * T,
                 # Per-host cap is adaptive (pool spread across the batch's distinct hosts),
                 # unless pinned via CRAWLER_HOST_CONCURRENCY. Report the pin or the range.
                 "host_concurrency": (
                     os.environ["CRAWLER_HOST_CONCURRENCY"]
                     if os.environ.get("CRAWLER_HOST_CONCURRENCY")
                     else f'{os.environ.get("CRAWLER_HOST_CONCURRENCY_FLOOR", "3")}'
                          f'-{os.environ.get("CRAWLER_HOST_CONCURRENCY_CEIL", "24")} adaptive')},
        "batch": {"running": bool(bs.get("running")), "total": bs.get("total", 0),
                  "done": bs.get("done", 0), "current": bs.get("current_job", ""),
                  "stopping": stop_requested() and bool(bs.get("running"))},
        "totals": agg,
    }


@app.get("/v1/audit")
def audit(limit: int = 100) -> dict:
    """Recent crawl-audit rows for careful (gov/mil) hosts — a provable record of polite crawling:
    what URL, when, as which UA, under which robots decision. Append-only (crawl_audit table)."""
    from crawler.dedup import CrawlHistory
    h = CrawlHistory()
    try:
        rows = h._conn.execute(
            "SELECT url, host, fetched_at, ua, robots_decision, status, reason, careful "
            "FROM crawl_audit ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
        return {"count": len(rows), "rows": [dict(r) for r in rows]}
    finally:
        h.close()


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    # no-store so the browser always fetches the current dashboard JS (avoids stale-cache
    # bugs where an old page reported wrong counts / ignored the freshness toggle).
    return HTMLResponse(
        dashboard.render(),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/v1/generate-jobs")
def generate_jobs_endpoint() -> dict:
    jobs = generate_jobs(_SEED)
    by_type = Counter(j.job_type for j in jobs)
    return {
        "count": len(jobs),
        "by_type_summary": ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items())),
        "jobs": [j.model_dump() for j in jobs],
    }


@app.post("/v1/crawl")
def crawl(req: CrawlRequest) -> dict:
    """Single job via async engine (same backend as /v1/crawl/batch)."""
    job = Job(**req.model_dump(exclude={"forward_to_ingest", "l2_ingest_url", "wayback"}))
    # Mark running so the dashboard STOP button lights up for single-job / Run-All-Jobs runs too
    # (Run All loops single crawls). The stop signal itself works for any run — the engine polls it.
    with _batch_lock:
        _batch_status.update(running=True, total=1, done=0, current_job=job.job_id)
    try:
        results = run_batch_async([job], forward=req.forward_to_ingest,
                                  l2_url=req.l2_ingest_url, seed=_SEED, kp=_KP, wayback=req.wayback)
    finally:
        with _batch_lock:
            _batch_status.update(running=False, done=1)
    return results[0] if results else {"job_id": job.job_id, "summary": {}, "documents": []}


class BatchRequest(BaseModel):
    jobs: list[Job]
    forward_to_ingest: bool = False
    l2_ingest_url: str | None = None
    wayback: bool = False          # C2 archival fallback (dashboard Wayback toggle); OFF = C1/C3 only


@app.post("/v1/crawl/batch")
def crawl_batch(req: BatchRequest) -> dict:
    """Batch crawl via async engine (8 browsers × 12 tabs = 96 concurrent pages)."""
    with _batch_lock:
        _batch_status.update(running=True, total=len(req.jobs), done=0,
                             current_job="(async pool)", results=[])
    try:
        results = run_batch_async(req.jobs, forward=req.forward_to_ingest,
                                  l2_url=req.l2_ingest_url, seed=_SEED, kp=_KP, wayback=req.wayback)
    finally:
        with _batch_lock:
            _batch_status.update(running=False, done=len(req.jobs),
                                 results=[{"job_id": r["job_id"], "fetched": r["summary"]["fetched"],
                                           "kept": r["summary"]["kept"], "sent": r["summary"]["sent"],
                                           "accepted": r["summary"]["accepted"]} for r in results])
    return {"jobs": len(results), "results": results}


@app.post("/v1/crawl/stop")
def crawl_stop() -> dict:
    """Halt the running crawl — signals the shared engine to tear down. Stops BOTH the live
    C1 render pool and the C3 CamoFox fallback (the engine drives every tier), so once pressed
    all crawling ceases. Idempotent: safe to call when nothing is running. The in-flight batch
    returns whatever it captured before the stop."""
    request_stop()
    with _batch_lock:
        was_running = bool(_batch_status.get("running"))
        if was_running:
            _batch_status["current_job"] = "(stopping…)"
    return {"stopping": was_running, "message": "stop signaled" if was_running else "no active batch"}


@app.get("/v1/batch/status")
def batch_status() -> dict:
    with _batch_lock:
        return dict(_batch_status)


class CheckKeywordsRequest(BaseModel):
    url: str
    keywords: list[str]
    render_js: bool = False


@app.post("/v1/check-keywords")
def check_keywords(req: CheckKeywordsRequest) -> dict:
    """Fetch one URL and report which keywords appear on it — the same
    word-boundary FlashText matching the Stage-2 gate uses. A relevance
    probe only: no BFS crawl, no dedup writes, no asset capture, no ingest
    POST. See scripts/check_keywords.py."""
    result = _check_keywords(req.url, req.keywords, render_js=req.render_js)
    result.pop("_text", None)
    result.pop("_title", None)
    return result


class CheckKeywordsBatchRequest(BaseModel):
    urls: list[str]
    keywords: list[str]
    render_js: bool = False
    parallel: int = 6


@app.post("/v1/check-keywords/batch")
def check_keywords_batch(req: CheckKeywordsBatchRequest) -> dict:
    """Probe MANY URLs against one keyword set in a single call — cheaply filter
    a candidate seed-URL list before committing crawl budget. Runs the same
    single-URL probe concurrently; results in input order."""
    from concurrent.futures import ThreadPoolExecutor

    def _probe(u: str) -> dict:
        r = _check_keywords(u, req.keywords, render_js=req.render_js)
        r.pop("_text", None)
        r.pop("_title", None)
        return r

    urls = req.urls
    if not urls:
        return {"count": 0, "results": []}
    workers = max(1, min(req.parallel, len(urls)))
    if workers == 1:
        results = [_probe(u) for u in urls]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_probe, urls))
    return {"count": len(results),
            "relevant": [r["url"] for r in results if r.get("matched")],
            "results": results}


class SuggestJobRequest(BaseModel):
    url: str
    target_entity: str | None = None
    job_type: str = "news"
    render_js: bool = False
    max_pages: int = 40
    max_depth: int = 2
    hunt_mode: str | None = None   # "focused" (probe-then-crawl) | "exhaustive"; None = manual budget


@app.post("/v1/suggest-job")
def suggest_job(req: SuggestJobRequest) -> dict:
    """Build a ready-to-run Job with probe-adaptive keywords: assemble a broad
    candidate pool from the seed (entity aliases + products + tech-domain terms),
    probe the URL, and keep only the pool terms that actually appear there. The
    returned job can be POSTed straight to /v1/crawl. If nothing hits, the seed
    is irrelevant for this pool — `keywords` comes back empty and `relevant` is
    false (skip the crawl)."""
    pool = _candidate_pool(_SEED, req.target_entity, req.job_type)
    disc = _discover_keywords(req.url, pool, render_js=req.render_js)
    selected = disc["selected_keywords"]
    job_kwargs: dict = dict(
        job_id=f"suggested_{req.job_type}_{(req.target_entity or 'x').lower()}",
        job_type=req.job_type, seed_urls=[req.url],
        keywords=selected, target_entity=req.target_entity, render_js=req.render_js,
    )
    if req.hunt_mode:
        # Let the hunt-mode preset own the crawl budget (don't pin max_pages/depth,
        # or model_fields_set would block the preset from filling them).
        job_kwargs["hunt_mode"] = req.hunt_mode
    else:
        job_kwargs["max_pages"] = req.max_pages
        job_kwargs["max_depth"] = req.max_depth
    job = Job(**job_kwargs)
    return {
        "relevant": bool(selected),
        "pool_size": disc["pool_size"],
        "selected_keywords": selected,
        "probe": {"status": disc["status"], "title": disc.get("title"),
                  "error": disc["error"]},
        "job": job.model_dump(),
    }


@app.get("/v1/schema")
def schema() -> dict:
    """The wire contract Layer 2 integrates against: the job input shape and
    the document output shape (JSON Schema)."""
    return {
        "job_input": Job.model_json_schema(),
        "document_output": Document.model_json_schema(),
        "ingest_endpoint": config.INGEST_API_PREFIX + "/page",
        "ingest_bundle": "{ \"document\": {...} }",
    }
