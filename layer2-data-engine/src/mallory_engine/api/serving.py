"""Interface B — the Serving API (L2 → L3).

Read-only. Every row is already scored, ranked, and "vs KSSL"; filters map to WHERE/ORDER BY on
pre-computed columns. The client never computes anything.
"""

from __future__ import annotations

import httpx

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..contracts.serving import (
    CompetitorSynthesisDTO,
    EvidenceRef,
    ExplainResponse,
    FieldExplanation,
    FieldPatternDTO,
    GeoEntry,
    InnovationCard,
    MatchupCard,
    MatchupSpec,
    OverviewMetrics,
    Page,
    PartnershipCard,
    PatentCard,
    SignalCard,
    SignalDetail,
    TenderCard,
    TenderMatch,
)
from ..db import get_db
from ..models.reference import RefCompetitor
from ..services.asset_client import fetch_asset
from ..models.serving import (
    SrvCompetitorSynthesis,
    SrvEvidence,
    SrvFieldPattern,
    SrvGeoEntry,
    SrvInnovation,
    SrvMatchup,
    SrvMatchupSpec,
    SrvOverviewMetrics,
    SrvPartnership,
    SrvPatent,
    SrvSignal,
    SrvSignalDetail,
    SrvTender,
    SrvTenderMatch,
)

router = APIRouter(prefix="/api/v1", tags=["serving"])


@router.get("/signals", response_model=Page[SignalCard])
def list_signals(
    db: Session = Depends(get_db),
    pillar: str = Query("competitive"),
    filter: str = Query("all"),
    company: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
) -> Page[SignalCard]:
    stmt = select(SrvSignal).where(SrvSignal.pillar == pillar)
    count_stmt = select(func.count()).select_from(SrvSignal).where(SrvSignal.pillar == pillar)
    if filter != "all":
        stmt = stmt.where(SrvSignal.dir == filter)
        count_stmt = count_stmt.where(SrvSignal.dir == filter)
    if company:
        stmt = stmt.where(SrvSignal.company == company)
        count_stmt = count_stmt.where(SrvSignal.company == company)

    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(SrvSignal.rank).offset((page - 1) * size).limit(size)
    ).all()
    return Page(items=[SignalCard.model_validate(r) for r in rows], page=page, size=size, total=total)


@router.get("/signals/{signal_id}/detail", response_model=SignalDetail)
def signal_detail(signal_id: int, db: Session = Depends(get_db)) -> SignalDetail:
    row = db.get(SrvSignalDetail, signal_id)
    if not row:
        raise HTTPException(404, "signal detail not found")
    return SignalDetail.model_validate(row)


# Which srv model carries confidence/provenance for each explainable target kind.
_CONF_MODEL = {"signal": SrvSignal}


@router.get("/explain/{target_kind}/{target_id}", response_model=ExplainResponse)
def explain(target_kind: str, target_id: str, db: Session = Depends(get_db)) -> ExplainResponse:
    """Why this? — the evidence chain + score decomposition behind any served row.

    Groups srv_evidence links by field; pulls confidence/provenance from the owning srv table.
    Works for rule-produced rows (llm_run_id NULL) and LLM-produced ones alike.
    """
    links = db.scalars(
        select(SrvEvidence)
        .where(SrvEvidence.target_kind == target_kind, SrvEvidence.target_id == str(target_id))
        .order_by(SrvEvidence.id)
    ).all()

    by_field: dict[str, FieldExplanation] = {}
    for lk in links:
        fe = by_field.get(lk.field)
        if fe is None:
            fe = FieldExplanation(field=lk.field, method=lk.method, evidence=[])
            by_field[lk.field] = fe
        fe.evidence.append(EvidenceRef(
            eid=lk.evidence_id, quote=lk.quote, source_url=lk.source_url,
            source_tier=lk.source_tier, published_at=lk.published_at, method=lk.method,
        ))

    resp = ExplainResponse(
        target_kind=target_kind, target_id=str(target_id),
        evidence_count=len(links), fields=list(by_field.values()),
    )
    model = _CONF_MODEL.get(target_kind)
    if model is not None:
        row = db.get(model, int(target_id) if target_id.isdigit() else target_id)
        if row is not None:
            resp.provenance = getattr(row, "provenance", "sourced")
            resp.confidence = getattr(row, "confidence", None)
            resp.confidence_band = getattr(row, "confidence_band", None)
            resp.confidence_parts = getattr(row, "confidence_parts", None)
    if not links and model is None:
        raise HTTPException(404, "no explanation for target")
    return resp


@router.get("/overview/{pillar}/metrics", response_model=OverviewMetrics)
def overview_metrics(pillar: str, db: Session = Depends(get_db)) -> OverviewMetrics:
    row = db.get(SrvOverviewMetrics, pillar)
    if not row:
        raise HTTPException(404, "metrics not computed for pillar")
    return OverviewMetrics.model_validate(row)


def _tender_with_matches(db: Session, t: SrvTender) -> TenderCard:
    matches = db.scalars(
        select(SrvTenderMatch)
        .where(SrvTenderMatch.tender_id == t.id)
        .order_by(SrvTenderMatch.fit_pct.desc())
    ).all()
    card = TenderCard.model_validate(t)
    card.matches = [TenderMatch.model_validate(m) for m in matches]
    return card


@router.get("/tenders", response_model=list[TenderCard])
def list_tenders(
    db: Session = Depends(get_db),
    filter: str = Query("all"),  # all|go|maybe|pass|closing
    category: str | None = None,
    sort: str = Query("deadline"),  # deadline|value
) -> list[TenderCard]:
    stmt = select(SrvTender)
    if filter in ("go", "maybe", "pass"):
        stmt = stmt.where(SrvTender.lean == filter)
    elif filter == "closing":
        stmt = stmt.where(SrvTender.status == "closing")
    if category:
        stmt = stmt.where(SrvTender.category == category)
    stmt = stmt.order_by(
        SrvTender.value_usd.desc().nullslast() if sort == "value"
        else SrvTender.deadline_date.asc().nullslast()
    )
    return [_tender_with_matches(db, t) for t in db.scalars(stmt).all()]


@router.get("/tenders/{tender_id}", response_model=TenderCard)
def tender_detail(tender_id: int, db: Session = Depends(get_db)) -> TenderCard:
    t = db.get(SrvTender, tender_id)
    if not t:
        raise HTTPException(404, "tender not found")
    return _tender_with_matches(db, t)


@router.get("/nav/counts")
def nav_counts(db: Session = Depends(get_db)) -> dict:
    """Per-view counts for the left-rail navigation."""

    def n(model, *where) -> int:
        stmt = select(func.count()).select_from(model)
        for w in where:
            stmt = stmt.where(w)
        return db.scalar(stmt) or 0

    return {
        "competitive": n(SrvSignal, SrvSignal.pillar == "competitive"),
        "market": n(SrvSignal, SrvSignal.pillar == "market"),
        "technology": n(SrvSignal, SrvSignal.pillar == "technology"),
        "matchups": n(SrvMatchup),
        "partnerships": n(SrvPartnership),
        "geo": n(SrvGeoEntry),
        "tenders": n(SrvTender),
        "innovation": n(SrvInnovation),
        "patents": n(SrvPatent),
    }


@router.get("/competitors")
def list_competitors(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(RefCompetitor).order_by(RefCompetitor.name)).all()
    return [
        {"id": c.id, "name": c.name, "hq": c.hq_country, "dir": c.threat_level,
         "is_anchor": c.is_anchor}
        for c in rows
    ]


# ── Positioning (matchups) ──


def _matchup_with_specs(db: Session, m: SrvMatchup) -> MatchupCard:
    specs = db.scalars(select(SrvMatchupSpec).where(SrvMatchupSpec.matchup_id == m.id)).all()
    card = MatchupCard.model_validate(m)
    card.specs = [MatchupSpec.model_validate(s) for s in specs]
    return card


@router.get("/matchups", response_model=list[MatchupCard])
def list_matchups(db: Session = Depends(get_db), category: str | None = None) -> list[MatchupCard]:
    stmt = select(SrvMatchup)
    if category:
        stmt = stmt.where(SrvMatchup.category == category)
    return [_matchup_with_specs(db, m) for m in db.scalars(stmt.order_by(SrvMatchup.edge_score)).all()]


@router.get("/matchups/{matchup_id}", response_model=MatchupCard)
def matchup_detail(matchup_id: int, db: Session = Depends(get_db)) -> MatchupCard:
    m = db.get(SrvMatchup, matchup_id)
    if not m:
        raise HTTPException(404, "matchup not found")
    return _matchup_with_specs(db, m)


# ── Geo ──


@router.get("/geo", response_model=list[GeoEntry])
def list_geo(
    db: Session = Depends(get_db), competitor: str | None = None, country: str | None = None
) -> list[GeoEntry]:
    stmt = select(SrvGeoEntry)
    if competitor:
        stmt = stmt.where(SrvGeoEntry.competitor_id == competitor)
    if country:
        stmt = stmt.where(SrvGeoEntry.country == country)
    return [GeoEntry.model_validate(g) for g in db.scalars(stmt.order_by(SrvGeoEntry.country)).all()]


# ── Partnerships ──


@router.get("/partnerships", response_model=list[PartnershipCard])
def list_partnerships(
    db: Session = Depends(get_db), competitor: str | None = None
) -> list[PartnershipCard]:
    stmt = select(SrvPartnership)
    if competitor:
        stmt = stmt.where(SrvPartnership.competitor_id == competitor)
    rows = db.scalars(stmt.order_by(SrvPartnership.competitor_name)).all()
    return [PartnershipCard.model_validate(p) for p in rows]


# ── Innovation ──


@router.get("/innovation", response_model=list[InnovationCard])
def list_innovation(db: Session = Depends(get_db), domain: str | None = None) -> list[InnovationCard]:
    stmt = select(SrvInnovation)
    if domain:
        stmt = stmt.where(SrvInnovation.tech_domain_id == domain)
    return [InnovationCard.model_validate(i) for i in db.scalars(stmt).all()]


# ── Patents ──


@router.get("/patents", response_model=list[PatentCard])
def list_patents(
    db: Session = Depends(get_db), competitor: str | None = None, domain: str | None = None
) -> list[PatentCard]:
    stmt = select(SrvPatent)
    if competitor:
        stmt = stmt.where(SrvPatent.competitor_id == competitor)
    if domain:
        stmt = stmt.where(SrvPatent.tech_domain_id == domain)
    return [PatentCard.model_validate(p) for p in db.scalars(stmt).all()]


# ── Synthesis + field patterns ──


@router.get("/competitors/{competitor_id}/synthesis", response_model=CompetitorSynthesisDTO)
def competitor_synthesis(competitor_id: str, db: Session = Depends(get_db)) -> CompetitorSynthesisDTO:
    row = db.get(SrvCompetitorSynthesis, competitor_id)
    if not row:
        raise HTTPException(404, "no synthesis for competitor")
    return CompetitorSynthesisDTO.model_validate(row)


@router.get("/field-patterns", response_model=list[FieldPatternDTO])
def field_patterns(db: Session = Depends(get_db)) -> list[FieldPatternDTO]:
    rows = db.scalars(select(SrvFieldPattern).order_by(SrvFieldPattern.ord)).all()
    return [FieldPatternDTO.model_validate(f) for f in rows]


@router.get("/asset-proxy")
def asset_proxy(storage_path: str):
    """Proxy an asset from the Layer 1 crawler's ingest API by its s3://... URI.

    L1 stores images, PDF attachments, and screenshots content-addressed under
    ``data/storage/`` and serves them via ``GET /artifact?path=...``. This
    endpoint relays the bytes to L3 so the browser never needs direct access to
    the crawler machine.

    **Usage from L3:**
    ``<img src="/api/v1/asset-proxy?storage_path=s3://mallory-raw/img/abc.jpg" />``
    """
    try:
        data = fetch_asset(storage_path)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code,
                            detail=f"asset not found: {storage_path}")
    except httpx.RequestError:
        raise HTTPException(status_code=502,
                            detail="crawler ingest API unreachable")
    return Response(content=data, media_type="application/octet-stream")
