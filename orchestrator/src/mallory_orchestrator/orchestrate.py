"""The control loop: catalog → jobs → crawler → L2, with coverage tracking. No human step."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crawler_client, jobgen
from .models import CoverageCell, JobRun, Source
from .seed import Seed, load_seed
from .sources import CATEGORY_TIER, resolve_source

FREQ_SECONDS = {"6h": 6 * 3600, "daily": 86400, "weekly": 7 * 86400}


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def upsert_source(
    db: Session,
    url: str,
    *,
    frequency: str = "daily",
    category: str | None = None,
    seed_urls: list[str] | None = None,
    search_template: str | None = None,
    region: str | None = None,
    added_by: str = "human",
) -> Source:
    """Add/update a source. Human supplies url+frequency+category; the rest is auto-resolved."""
    human_cat = {resolve_source(url).domain: category} if category else {}
    res = resolve_source(url, human_catalog=human_cat)
    existing = db.get(Source, res.domain)
    row = existing or Source(domain=res.domain, created_at=_now())
    row.source_id = res.source_id
    row.category = res.category
    row.tier = CATEGORY_TIER[res.category]
    row.frequency = frequency
    row.region = region
    row.source_known = res.source_known
    row.tier_origin = res.resolved_by
    row.added_by = added_by
    if seed_urls is not None:
        row.seed_urls = seed_urls
    if search_template is not None:
        row.search_template = search_template
    if not existing:
        db.add(row)
    db.commit()
    return row


def due_sources(db: Session) -> list[Source]:
    out = []
    now = _now()
    for s in db.scalars(select(Source)).all():
        if s.last_crawled is None:
            out.append(s)
            continue
        age = (now - s.last_crawled).total_seconds()
        if age >= FREQ_SECONDS.get(s.frequency, 86400):
            out.append(s)
    return out


def build_jobs(db: Session, seed: Seed | None = None, only_due: bool = False) -> list[dict]:
    seed = seed or load_seed()
    sources = due_sources(db) if only_due else db.scalars(select(Source)).all()
    return jobgen.generate(list(sources), seed)


def build_test_jobs(seed_dir: str | None = None) -> list[dict]:
    """Offline end-to-end batch: explicit fixture URLs with correct job_type, source-stamped."""
    import json
    import pathlib

    from .config import get_settings
    from .sources import resolve_source

    d = pathlib.Path(seed_dir or get_settings().seed_dir)
    targets = json.loads((d / "test_targets.json").read_text()).get("targets", [])
    jobs: list[dict] = []
    for i, t in enumerate(targets, 1):
        res = resolve_source(t["url"])
        job = jobgen._base(t["job_type"], f"test_{i}_{res.source_id}", [t["url"]],
                           t.get("keywords", []), t.get("target_entity"))
        job["expected_record_types"] = t.get("expected_record_types", job["expected_record_types"])
        job["source_id"] = res.source_id
        job["source_tier"] = res.tier
        job["source_type"] = res.category
        jobs.append(job)
    return jobs


def _mark_coverage(db: Session, job: dict) -> None:
    domain_id = job.get("source_id")
    entity = job.get("target_entity")
    # find the domain by source_id
    src = db.scalar(select(Source).where(Source.source_id == domain_id))
    if not src:
        return
    src.last_crawled = _now()
    cell_id = f"{src.domain}|{entity or '*'}"
    cell = db.get(CoverageCell, cell_id) or CoverageCell(id=cell_id, domain=src.domain, entity_id=entity)
    cell.last_fetched = _now()
    cell.status = "fresh"
    db.merge(cell)


def run_batch(db: Session, jobs: list[dict], *, forward: bool = True, process: bool = True) -> dict:
    totals = {"dispatched": 0, "fetched": 0, "kept": 0, "records": 0, "forwarded": 0,
              "accepted": 0, "errors": 0}
    for job in jobs:
        run = JobRun(job_id=job["job_id"], domain=None, entity_id=job.get("target_entity"),
                     dispatched_at=_now())
        try:
            resp = crawler_client.dispatch(job)
            summ = resp.get("summary", {})
            run.fetched = summ.get("fetched", 0)
            run.kept = summ.get("kept", 0)
            run.records_emitted = summ.get("records_emitted", 0)
            if forward:
                fwd, acc = crawler_client.forward_to_l2(resp)
                run.records_forwarded, run.l2_accepted = fwd, acc
                totals["forwarded"] += fwd
                totals["accepted"] += acc
            run.status = "ok"
            totals["fetched"] += run.fetched
            totals["kept"] += run.kept
            totals["records"] += run.records_emitted
            _mark_coverage(db, job)
        except Exception as e:  # noqa: BLE001 — record and continue at scale
            run.status = "error"
            run.detail = str(e)[:400]
            totals["errors"] += 1
        totals["dispatched"] += 1
        db.add(run)
    db.commit()

    if process and totals["accepted"] > 0:
        try:
            totals["l2_pipeline"] = crawler_client.trigger_l2_pipeline()
        except Exception as e:  # noqa: BLE001
            totals["l2_pipeline_error"] = str(e)[:200]
    return totals
