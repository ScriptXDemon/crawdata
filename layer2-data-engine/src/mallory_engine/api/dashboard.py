"""Live intelligence dashboard — a single consolidated data endpoint + the HTML page.

Unlike the static snapshot, this reads the running database, so the dashboard always reflects
current state (including freshly-crawled pages once the pipeline runs). One endpoint returns
everything the page needs to avoid a dozen round-trips.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models.graph import KgEdge, KgNode, SrvGraphInsight
from ..models.serving import (
    SrvCompetitorSynthesis,
    SrvEvidence,
    SrvFieldPattern,
    SrvMatchup,
    SrvMatchupSpec,
    SrvSignal,
    SrvTender,
    SrvTenderMatch,
)

router = APIRouter(tags=["dashboard"])

# api/dashboard.py → api → mallory_engine → src → layer2-data-engine/  (static lives here)
_STATIC = Path(__file__).resolve().parents[3] / "static"


def _layout(nodes: list[KgNode], edges: list[KgEdge]) -> dict[str, tuple[float, float]]:
    """Deterministic circular-by-kind layout — no numpy/networkx needed at request time."""
    import math

    kinds: dict[str, list[str]] = {}
    for n in nodes:
        kinds.setdefault(n.kind, []).append(n.id)
    # place each kind on its own ring
    ring_r = {"competitor": 0.55, "org": 0.9, "country": 0.9, "product": 0.28,
              "tender": 0.7, "signal": 0.4, "patent": 0.2}
    pos: dict[str, tuple[float, float]] = {}
    for kind, ids in kinds.items():
        r = ring_r.get(kind, 0.75)
        for i, nid in enumerate(sorted(ids)):
            ang = 2 * math.pi * i / max(len(ids), 1) + hash(kind) % 100 / 100
            pos[nid] = (round(r * math.cos(ang), 4), round(r * math.sin(ang), 4))
    return pos


@router.get("/api/v1/dashboard/data", summary="Everything the live dashboard renders")
def dashboard_data(db: Session = Depends(get_db)) -> dict:
    evidence: dict[str, list] = {}
    for e in db.scalars(select(SrvEvidence)).all():
        evidence.setdefault(f"{e.target_kind}:{e.target_id}", []).append(
            {"field": e.field, "eid": e.evidence_id, "quote": e.quote, "url": e.source_url,
             "tier": e.source_tier, "method": e.method})

    knodes = db.scalars(select(KgNode)).all()
    kedges = db.scalars(select(KgEdge)).all()
    pos = _layout(knodes, kedges)

    def _syn(r: SrvCompetitorSynthesis) -> dict:
        return {"id": r.competitor_id, "name": r.competitor_name, "thesis": r.thesis,
                "sowhat": r.strat_sowhat, "vulnerabilities": r.vulnerabilities,
                "predictions": r.predictions, "moves": r.moves, "gaps": r.gaps,
                "provenance": r.provenance, "confidence": r.confidence, "band": r.confidence_band}

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "signals": [
            {"id": r.id, "pillar": r.pillar, "dir": r.dir, "rank": r.rank, "title": r.title,
             "meta": r.meta, "company": r.company, "lens": r.lens, "sowhat": r.sowhat,
             "ago": r.ago_display, "confidence": r.confidence, "band": r.confidence_band,
             "parts": r.confidence_parts, "corroboration": r.corroboration,
             "provenance": r.provenance, "url": r.source_url}
            for r in db.scalars(select(SrvSignal).order_by(SrvSignal.pillar, SrvSignal.rank)).all()
        ],
        "tenders": [
            {"id": t.id, "title": t.title, "issuer": t.issuer, "country": t.country,
             "category": t.category, "dl_days": t.dl_days, "lean": t.lean,
             "lean_text": t.lean_text, "status": t.status,
             "matches": [{"name": m.kssl_product_name, "fit_pct": m.fit_pct,
                          "level": m.fit_level, "lines": m.match_lines}
                         for m in db.scalars(select(SrvTenderMatch)
                                             .where(SrvTenderMatch.tender_id == t.id)
                                             .order_by(SrvTenderMatch.fit_pct.desc())).all()]}
            for t in db.scalars(select(SrvTender)).all()
        ],
        "matchups": [
            {"id": m.id, "kssl": m.kssl_name, "comp": m.comp_name, "by": m.comp_by,
             "category": m.category, "dir": m.dir, "edge": m.edge_score, "verdict": m.verdict,
             "method": m.verdict_method, "parts": m.edge_parts,
             "specs": [{"label": sp.spec_label, "kssl": sp.kssl_value, "comp": sp.comp_value,
                        "leader": sp.leader}
                       for sp in db.scalars(select(SrvMatchupSpec)
                                            .where(SrvMatchupSpec.matchup_id == m.id)).all()]}
            for m in db.scalars(select(SrvMatchup).order_by(SrvMatchup.edge_score)).all()
        ],
        "synthesis": [_syn(r) for r in db.scalars(select(SrvCompetitorSynthesis)).all()
                      if r.thesis],
        "field_patterns": [
            {"title": r.title, "summary": r.summary, "bottom_line": r.bottom_line,
             "provenance": r.provenance}
            for r in db.scalars(select(SrvFieldPattern).order_by(SrvFieldPattern.ord)).all()
        ],
        "insights": [
            {"kind": r.kind, "dir": r.dir, "title": r.title, "sowhat": r.sowhat,
             "entities": r.entities, "metric": float(r.metric or 0)}
            for r in db.scalars(select(SrvGraphInsight).order_by(SrvGraphInsight.rank)).all()
        ],
        "graph": {
            "nodes": [{"id": n.id, "kind": n.kind, "label": n.label,
                       "x": pos.get(n.id, (0, 0))[0], "y": pos.get(n.id, (0, 0))[1],
                       "community": n.community_id, "degree": n.degree,
                       "dir": (n.attrs or {}).get("dir"),
                       "anchor": bool((n.attrs or {}).get("is_anchor"))}
                      for n in knodes],
            "edges": [{"src": e.src_id, "dst": e.dst_id, "rel": e.rel, "sub": e.rel_subtype,
                       "prov": e.provenance} for e in kedges],
        },
        "evidence": evidence,
        "stats": {
            "nodes": len(knodes), "edges": len(kedges),
            "signals": db.scalar(select(func.count()).select_from(SrvSignal)) or 0,
            "sourced_synth": db.scalar(
                select(func.count()).select_from(SrvCompetitorSynthesis)
                .where(SrvCompetitorSynthesis.provenance == "sourced")) or 0,
            "evidence_links": db.scalar(select(func.count()).select_from(SrvEvidence)) or 0,
            "provider": "live", "model": "mallory L2",
        },
    }


@router.get("/dashboard", include_in_schema=False)
def dashboard_page():
    """The live dashboard page (fetches /api/v1/dashboard/data same-origin)."""
    path = _STATIC / "dashboard_live.html"
    if not path.exists():
        return PlainTextResponse("dashboard_live.html not found under static/", status_code=404)
    return FileResponse(path, media_type="text/html")
