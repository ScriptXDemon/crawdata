"""Per-job orchestration: HARVEST -> FILTER -> self-dedup -> EXTRACT -> INGEST.

This is the engine that runs one crawl job end-to-end and returns a structured
result. The self-dedup step (§7A) is the gate that stops "daily crawl, no
change" from flooding L2: a kept page is emitted only when its URL is new OR its
content_hash changed since our last crawl.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from . import extract, gate
from .dedup import CrawlHistory, classify
from .fetcher import Fetcher
from .harvest import harvest
from .ingest_client import IngestOutcome
from .models import Document, Job
from .resolver import build_matcher
from .seed import Seed, load_seed


@dataclass
class JobResult:
    job_id: str
    fetched: int = 0
    not_modified: int = 0          # 304 — skipped before download
    dropped_by_gate: int = 0
    skipped_unchanged: int = 0     # self-dedup: content_hash unchanged since last crawl
    skipped_duplicate: int = 0     # same-run dedup: content_hash matches an earlier page
    kept: int = 0
    documents: list[Document] = field(default_factory=list)
    sent: int = 0                  # one page bundle sent per kept document
    accepted: int = 0
    rejected: int = 0
    outcomes: list[IngestOutcome] = field(default_factory=list)
    gate_reasons: dict = field(default_factory=dict)
    errors: int = 0
    errors_by_reason: dict = field(default_factory=dict)
    trap_skipped: int = 0

    def bump_reason(self, reason: str) -> None:
        self.gate_reasons[reason] = self.gate_reasons.get(reason, 0) + 1


def _capture_defaults(seed: Seed) -> dict:
    return seed.capture_defaults


def run_job(job: Job, ingest_client, seed: Seed | None = None,
            history: CrawlHistory | None = None, matcher=None,
            on_fetch: Callable[[dict], None] | None = None,
            on_page: Callable[[dict], None] | None = None) -> JobResult:
    seed = seed or load_seed()
    matcher = matcher or build_matcher(seed)
    own_history = history is None
    history = history or CrawlHistory()
    caps = _capture_defaults(seed)

    fetcher = Fetcher(
        user_agent=caps["user_agent"],
        timeout_s=caps.get("timeout_seconds", 30),
        delay_s=caps.get("crawl_delay_seconds", 2),
        max_retries=caps.get("max_retries", 2),
        render_js=job.render_js,
        respect_robots=caps.get("respect_robots_txt", True),
        interaction_cfg=job.interaction,
        # grab the screenshot inline during the render pass (no 2nd browser)
        screenshot_wanted=("screenshot" in job.capture and job.render_js),
    )

    result = JobResult(job_id=job.job_id)
    pages, hstats = harvest(job, fetcher, history, on_fetch=on_fetch)
    result.fetched = hstats.fetched
    result.not_modified = hstats.not_modified
    result.errors = hstats.errors
    result.errors_by_reason = hstats.errors_by_reason
    result.trap_skipped = hstats.trap_skipped

    # Same-run content dedup: an SPA that bounces every sub-path back to the
    # same rendered shell (redirect-to-home, or a client route that never
    # actually swaps content) must not be emitted once per URL — only the
    # first page with a given content_hash in this run is kept.
    seen_hashes: set[str] = set()

    for idx, page in enumerate(pages):
        t_start = time.perf_counter()
        # Cheap core build (text + entities + metadata) — no assets yet.
        doc = extract.build_document(job, page, seed, matcher, fetcher, enrich=False)
        if doc is None:
            result.bump_reason("no_main_text")
            if on_page:
                on_page({"n": idx + 1, "total": len(pages), "url": page.url,
                         "depth": page.depth, "stage": "dropped", "reason": "no_main_text",
                         "elapsed": time.perf_counter() - t_start})
            continue

        # Stage 2 — mechanical gate.
        g = gate.evaluate(job, doc.title, doc.main_text, doc.entities_detected,
                          doc.published_at)
        result.bump_reason(g.reason)
        if not g.keep:
            result.dropped_by_gate += 1
            if on_page:
                on_page({"n": idx + 1, "total": len(pages), "url": page.url,
                         "depth": page.depth, "stage": "dropped", "reason": g.reason,
                         "elapsed": time.perf_counter() - t_start})
            continue

        # Self-dedup (§7A): emit only if new or content changed since last crawl.
        stored = history.get(page.url)
        verdict = classify(stored, status=page.fetch.status,
                          content_hash=doc.content_hash)
        history.upsert(page.url, content_hash=doc.content_hash,
                      etag=page.fetch.etag, last_modified=page.fetch.last_modified,
                      status=page.fetch.status, fetched_at=page.fetch.fetched_at,
                      js_heavy=job.render_js)
        if verdict == "unchanged":
            result.skipped_unchanged += 1
            if on_page:
                on_page({"n": idx + 1, "total": len(pages), "url": page.url,
                         "depth": page.depth, "stage": "skipped", "reason": "dedup_unchanged",
                         "elapsed": time.perf_counter() - t_start})
            continue

        if doc.content_hash in seen_hashes:
            result.skipped_duplicate += 1
            if on_page:
                on_page({"n": idx + 1, "total": len(pages), "url": page.url,
                         "depth": page.depth, "stage": "skipped", "reason": "dedup_duplicate_in_run",
                         "elapsed": time.perf_counter() - t_start})
            continue
        seen_hashes.add(doc.content_hash)

        result.kept += 1
        # Now enrich the KEPT doc with expensive assets, then send one bundle.
        extract.enrich_assets(job, doc, page, fetcher)
        result.documents.append(doc)

        result.sent += 1
        outcome = ingest_client.send(doc)
        result.outcomes.append(outcome)
        if outcome.accepted:
            result.accepted += 1
        else:
            result.rejected += 1

        if on_page:
            on_page({"n": idx + 1, "total": len(pages), "url": page.url,
                     "depth": page.depth, "stage": "kept", "reason": g.reason,
                     "sent": result.sent, "accepted": result.accepted,
                     "elapsed": time.perf_counter() - t_start})

    if own_history:
        history.close()
    return result


def run_batch(jobs: list[Job], ingest_client, seed: Seed | None = None,
              history: CrawlHistory | None = None) -> list[JobResult]:
    seed = seed or load_seed()
    matcher = build_matcher(seed)
    own_history = history is None
    history = history or CrawlHistory()
    results = [run_job(j, ingest_client, seed, history, matcher) for j in jobs]
    if own_history:
        history.close()
    return results
