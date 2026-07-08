"""Internal ops endpoints — run the pipeline and inspect processing state (the monitor's data)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models.serving import SrvSignal, SrvTender
from ..models.staging import StgSignal, StgTender
from ..pipeline.runner import process_pending
from ..services import (
    competitor_synthesis,
    field_patterns,
    graph_analytics,
    graph_builder,
    matchup_synthesis,
    multimodal,
)
from ..services.llm import get_llm

router = APIRouter(prefix="/ops", tags=["ops"])


@router.post("/process", summary="Run the pipeline over pending staging rows")
def run_pipeline(db: Session = Depends(get_db)) -> dict:
    result = process_pending(db)
    return {
        "signals_processed": result.signals_processed,
        "tenders_processed": result.tenders_processed,
        "partnerships_processed": result.partnerships_processed,
        "geo_processed": result.geo_processed,
        "innovation_processed": result.innovation_processed,
    }


@router.post("/rebuild-graph", summary="Rebuild the knowledge graph + run pattern analytics")
def rebuild_graph(db: Session = Depends(get_db)) -> dict:
    counts = graph_builder.rebuild_graph(db)
    analytics = graph_analytics.run_analytics(db)
    db.commit()
    return {**counts, **analytics}


@router.post("/recompute-matchups", summary="S-22: rebuild srv_matchups from ref_matchups")
def recompute_matchups(db: Session = Depends(get_db)) -> dict:
    n = matchup_synthesis.recompute_all(db, get_llm(db=db))
    db.commit()
    return {"matchups": n}


@router.post("/synthesize", summary="S-23: competitor synthesis (all, or one via ?competitor=)")
def synthesize(competitor: str | None = None, db: Session = Depends(get_db)) -> dict:
    llm = get_llm(db=db)
    if competitor:
        results = [competitor_synthesis.synthesize_competitor(db, llm, competitor)]
    else:
        results = competitor_synthesis.synthesize_all(db, llm)
    db.commit()
    return {"results": results}


@router.post("/field-patterns", summary="S-24: recompute cross-field patterns")
def refresh_patterns(db: Session = Depends(get_db)) -> dict:
    result = field_patterns.refresh_field_patterns(db, get_llm(db=db))
    db.commit()
    return result


@router.post("/analyze-assets", summary="Multimodal: caption images + extract PDF specs (vision swaps in)")
def analyze_assets(db: Session = Depends(get_db)) -> dict:
    # opt-in because the vision model must swap into VRAM; keep it off the hot path.
    return multimodal.analyze_pending_assets(db, get_llm(db=db))


@router.get("/status", summary="Processing-state counts (feeds the monitor view)")
def status(db: Session = Depends(get_db)) -> dict:
    def by_status(model) -> dict[str, int]:
        rows = db.execute(
            select(model.proc_status, func.count()).group_by(model.proc_status)
        ).all()
        return {s: n for s, n in rows}

    return {
        "staging": {"signals": by_status(StgSignal), "tenders": by_status(StgTender)},
        "serving": {
            "signals": db.scalar(select(func.count()).select_from(SrvSignal)) or 0,
            "tenders": db.scalar(select(func.count()).select_from(SrvTender)) or 0,
        },
    }
