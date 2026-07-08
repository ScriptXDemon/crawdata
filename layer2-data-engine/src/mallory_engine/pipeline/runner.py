"""Process pending staging rows into the serving tables, then recompute ranks + metrics.

Idempotent: only rows with ``proc_status='received'`` are processed; publishing uses upserts, so a
re-run is safe. In production these stages run as event-driven workers (see the service catalog);
here a single callable keeps the flow simple and testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.staging import StgGeo, StgInnovation, StgPartnership, StgSignal, StgTender
from ..services import (
    corroboration,
    domain_pipeline,
    extraction,
    graph_analytics,
    graph_builder,
    metrics,
    signal_pipeline,
    tender_scoring,
)
from ..services.llm import LLMProvider, get_llm


@dataclass
class PipelineResult:
    signals_processed: int
    tenders_processed: int
    partnerships_processed: int
    geo_processed: int
    innovation_processed: int


def process_pending(db: Session, llm: LLMProvider | None = None) -> PipelineResult:
    # db-bound so the cache + llm_runs ledger record every call (the scheduler/ops path
    # previously passed no db-bound llm → no ledger; this fixes it).
    llm = llm or get_llm(db=db)

    # ST-1 extraction: bare documents (the crawler's normal mode) → typed staging records.
    # LLM-primary (fast model) with regex fallback; stub/offline ⇒ pure regex, unchanged.
    extraction.extract_pending(db, llm)
    db.flush()

    # Corroboration counts across ALL signals (not just this batch) — a claim's independent-
    # source count reflects the full corpus, so re-runs recompute it as new sources arrive.
    corr = corroboration.corroboration_counts(db)

    pending_signals = db.scalars(
        select(StgSignal).where(StgSignal.proc_status == "received")
    ).all()
    for ss in pending_signals:
        signal_pipeline.process_signal(db, llm, ss, corroboration=corr.get(ss.id, 1))

    pending_tenders = db.scalars(
        select(StgTender).where(StgTender.proc_status == "received")
    ).all()
    for st in pending_tenders:
        tender_scoring.process_tender(db, llm, st)

    pending_parts = db.scalars(
        select(StgPartnership).where(StgPartnership.proc_status == "received")
    ).all()
    for sp in pending_parts:
        domain_pipeline.process_partnership(db, llm, sp)

    pending_geo = db.scalars(select(StgGeo).where(StgGeo.proc_status == "received")).all()
    for sg in pending_geo:
        domain_pipeline.process_geo(db, llm, sg)

    pending_innov = db.scalars(
        select(StgInnovation).where(StgInnovation.proc_status == "received")
    ).all()
    for si in pending_innov:
        domain_pipeline.process_innovation(db, llm, si)

    # Flush merges so the rank/metrics queries below see the freshly published rows
    # (the session runs with autoflush disabled).
    db.flush()
    signal_pipeline.recompute_ranks(db)
    metrics.build_overview_metrics(db)
    # Knowledge graph: full reconcile (pure projection — cheap at this scale, self-healing).
    # ponytail: incremental edge appends when row volume makes full rebuilds slow.
    graph_builder.rebuild_graph(db)
    graph_analytics.run_analytics(db)

    db.commit()
    return PipelineResult(
        len(pending_signals), len(pending_tenders), len(pending_parts),
        len(pending_geo), len(pending_innov),
    )
