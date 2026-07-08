"""Admin API — the only human control surface (add sources with url+frequency+category)."""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import orchestrate
from .db import get_db
from .models import CoverageCell, JobRun, Source

router = APIRouter(prefix="/api")


class AddSource(BaseModel):
    url: str
    frequency: str = "daily"
    category: str | None = None  # optional; auto-classified if omitted


def _src_dto(s: Source) -> dict:
    return {
        "domain": s.domain, "source_id": s.source_id, "category": s.category, "tier": s.tier,
        "frequency": s.frequency, "source_known": s.source_known, "tier_origin": s.tier_origin,
        "added_by": s.added_by, "region": s.region,
        "accept_rate": s.accept_rate,
        "last_crawled": s.last_crawled.isoformat() if s.last_crawled else None,
    }


@router.get("/sources")
def list_sources(db: Session = Depends(get_db)) -> list[dict]:
    return [_src_dto(s) for s in db.scalars(select(Source).order_by(Source.tier, Source.domain)).all()]


@router.post("/sources")
def add_source(body: AddSource, db: Session = Depends(get_db)) -> dict:
    s = orchestrate.upsert_source(db, body.url, frequency=body.frequency, category=body.category)
    return _src_dto(s)


@router.get("/coverage")
def coverage(db: Session = Depends(get_db)) -> dict:
    sources = db.scalars(select(Source)).all()
    cells = db.scalars(select(CoverageCell)).all()
    never = sum(1 for s in sources if s.last_crawled is None)
    fresh = sum(1 for c in cells if c.status == "fresh")
    discovered = sum(1 for s in sources if s.added_by == "auto")
    return {
        "sources": len(sources),
        "coverage_cells": len(cells),
        "fresh": fresh,
        "gaps": never,
        "auto_discovered": discovered,
    }


@router.get("/jobs")
def jobs(db: Session = Depends(get_db)) -> list[dict]:
    runs = db.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(50)).all()
    return [
        {"job_id": r.job_id, "entity": r.entity_id, "fetched": r.fetched, "kept": r.kept,
         "records": r.records_emitted, "forwarded": r.records_forwarded, "l2_accepted": r.l2_accepted,
         "status": r.status, "detail": r.detail,
         "at": r.dispatched_at.isoformat() if r.dispatched_at else None}
        for r in runs
    ]


@router.get("/matrix")
def matrix(db: Session = Depends(get_db)) -> dict:
    jb = orchestrate.build_jobs(db, only_due=False)
    return {"total_jobs": len(jb), "by_type": dict(Counter(j["job_type"] for j in jb))}


@router.post("/run")
def run(db: Session = Depends(get_db)) -> dict:
    """Production: dispatch due catalog jobs (real network)."""
    return orchestrate.run_batch(db, orchestrate.build_jobs(db, only_due=True))


@router.post("/run/test")
def run_test(db: Session = Depends(get_db)) -> dict:
    """Offline end-to-end: dispatch the fixture batch through crawler → L2."""
    return orchestrate.run_batch(db, orchestrate.build_test_jobs())
