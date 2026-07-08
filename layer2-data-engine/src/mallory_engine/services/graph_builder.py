"""Graph builder — projects the relational system of record into kg_nodes/kg_edges.

Full idempotent rebuild: wipe and re-derive everything from ref_*/srv_* tables, so the graph
is always reproducible and incremental bugs self-heal. Every edge carries provenance,
confidence, and evidence links (srv_evidence, target_kind='kg_edge') back to the exact rows
that produced it. All deterministic — no LLM anywhere in the graph itself.
"""

from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models.graph import KgEdge, KgNode
from ..models.reference import RefCompetitor, RefCompetitorProduct, RefKsslProduct, RefMatchup
from ..models.serving import (
    SrvEvidence,
    SrvGeoEntry,
    SrvPartnership,
    SrvPatent,
    SrvSignal,
    SrvTender,
    SrvTenderMatch,
)
from .evidence import EvidenceItem, write_evidence

_CONF_BY_PROV = {"sourced": 70, "estimate": 40, "analyst": 60}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


class _Builder:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.nodes: dict[str, KgNode] = {}

    def node(self, kind: str, key: str, label: str, *, ref_table: str | None = None,
             ref_id: str | None = None, attrs: dict | None = None,
             provenance: str = "sourced") -> str:
        nid = f"{kind}:{key}"
        if nid not in self.nodes:
            self.nodes[nid] = KgNode(id=nid, kind=kind, label=label, ref_table=ref_table,
                                     ref_id=ref_id, attrs=attrs, provenance=provenance)
        return nid

    def edge(self, src: str, dst: str, rel: str, *, subtype: str = "",
             attrs: dict | None = None, provenance: str = "sourced",
             confidence: int | None = None, first_seen: dt.datetime | None = None,
             evidence: list[EvidenceItem] | None = None) -> None:
        e = KgEdge(
            src_id=src, dst_id=dst, rel=rel, rel_subtype=subtype, weight=1,
            confidence=confidence if confidence is not None else _CONF_BY_PROV.get(provenance, 40),
            provenance=provenance, attrs=attrs, first_seen=first_seen,
        )
        self.db.add(e)
        if evidence:
            self.db.flush()
            write_evidence(self.db, target_kind="kg_edge", target_id=e.id,
                           items=[("edge", item) for item in evidence], method="rule")


def rebuild_graph(db: Session) -> dict[str, int]:
    """Wipe and re-derive the whole graph. Returns {nodes, edges}."""
    db.execute(delete(SrvEvidence).where(SrvEvidence.target_kind == "kg_edge"))
    db.query(KgEdge).delete()
    db.query(KgNode).delete()
    db.flush()

    b = _Builder(db)

    # ── competitors (incl. the KSSL anchor) and their products: makes ──
    for c in db.scalars(select(RefCompetitor)).all():
        b.node("competitor", c.id, c.name, ref_table="ref_competitors", ref_id=c.id,
               attrs={"dir": c.threat_level, "is_anchor": c.is_anchor, "hq": c.hq_country})
    anchor = db.scalars(select(RefCompetitor).where(RefCompetitor.is_anchor.is_(True))).first()
    for p in db.scalars(select(RefKsslProduct)).all():
        pid = b.node("product", p.id, p.name, ref_table="ref_kssl_products", ref_id=p.id,
                     attrs={"category": p.category_id, "side": "kssl"})
        if anchor:
            b.edge(f"competitor:{anchor.id}", pid, "makes")
    for cp in db.scalars(select(RefCompetitorProduct)).all():
        pid = b.node("product", cp.id, cp.name, ref_table="ref_competitor_products", ref_id=cp.id,
                     attrs={"category": cp.category_id, "side": "competitor"})
        if f"competitor:{cp.competitor_id}" in b.nodes:
            b.edge(f"competitor:{cp.competitor_id}", pid, "makes")

    # ── partnerships: competitor -[partners_with:{rel_type}]-> org ──
    for pt in db.scalars(select(SrvPartnership)).all():
        if not pt.competitor_id or f"competitor:{pt.competitor_id}" not in b.nodes:
            continue
        oid = b.node("org", _slug(pt.partner_name), pt.partner_name,
                     attrs={"kind": pt.partner_kind, "country": pt.country})
        b.edge(
            f"competitor:{pt.competitor_id}", oid, "partners_with",
            subtype=pt.rel_type or "", provenance=pt.provenance,
            attrs={"deal_value": pt.deal_value, "kssl_relevance": pt.kssl_relevance},
            evidence=[EvidenceItem(eid=f"part:{pt.id}", kind="partnership",
                                   text=f"{pt.competitor_name} × {pt.partner_name} ({pt.rel_type})",
                                   source_url=pt.source_url)],
        )

    # ── geo: competitor -[present_in:{stage}]-> country ──
    for g in db.scalars(select(SrvGeoEntry)).all():
        if not g.competitor_id or not g.country:
            continue
        if f"competitor:{g.competitor_id}" not in b.nodes:
            continue
        cid = b.node("country", _slug(g.country), g.country)
        b.edge(
            f"competitor:{g.competitor_id}", cid, "present_in",
            subtype=g.stage or "", provenance=g.provenance,
            attrs={"product": g.product_name, "value": g.contract_value},
            evidence=[EvidenceItem(eid=f"geo:{g.id}", kind="geo",
                                   text=f"{g.competitor_name} in {g.country}: {g.product_name}",
                                   source_url=g.source_url)],
        )

    # ── matchups: kssl product -[competes_with:{edge_score}]-> competitor product ──
    for mu in db.scalars(select(RefMatchup)).all():
        if not mu.kssl_product_id or f"product:{mu.kssl_product_id}" not in b.nodes:
            continue
        comp_pid = b.node("product", _slug(mu.comp_name), mu.comp_name,
                          attrs={"category": mu.category_id, "side": "competitor", "by": mu.comp_by})
        b.edge(f"product:{mu.kssl_product_id}", comp_pid, "competes_with",
               provenance="estimate",
               attrs={"category": mu.category_id},
               evidence=[EvidenceItem(eid=f"ref:ref_matchups:{mu.id}", kind="ref",
                                      text=f"{mu.kssl_name} vs {mu.comp_name}")])

    # ── tenders: tender -[issued_in]-> country; kssl product -[fits:{fit_pct}]-> tender ──
    for t in db.scalars(select(SrvTender)).all():
        tid = b.node("tender", str(t.id), t.title,
                     attrs={"category": t.category, "lean": t.lean, "status": t.status})
        if t.country:
            b.edge(tid, b.node("country", _slug(t.country), t.country), "issued_in",
                   provenance=t.provenance)
        for m in db.scalars(select(SrvTenderMatch).where(SrvTenderMatch.tender_id == t.id)).all():
            if m.kssl_product_id and f"product:{m.kssl_product_id}" in b.nodes:
                b.edge(f"product:{m.kssl_product_id}", tid, "fits",
                       attrs={"fit_pct": m.fit_pct, "fit_level": m.fit_level},
                       evidence=[EvidenceItem(eid=f"tender:{t.id}", kind="tender",
                                              text=f"{m.kssl_product_name} fits {m.fit_pct}%",
                                              source_url=t.source_url)])

    # ── signals: signal -[about]-> competitor ──
    comp_by_name = {c.name: c.id for c in db.scalars(select(RefCompetitor)).all()}
    for s in db.scalars(select(SrvSignal)).all():
        comp_id = comp_by_name.get(s.company or "")
        if not comp_id:
            continue
        sid = b.node("signal", str(s.id), s.title,
                     attrs={"dir": s.dir, "pillar": s.pillar, "confidence": s.confidence})
        b.edge(sid, f"competitor:{comp_id}", "about", provenance=s.provenance,
               confidence=s.confidence, first_seen=s.published_at,
               evidence=[EvidenceItem(eid=f"sig:{s.id}", kind="signal", text=s.title,
                                      source_url=s.source_url, published_at=s.published_at)])

    # ── patents: competitor -[filed]-> patent ──
    for p in db.scalars(select(SrvPatent)).all():
        if not p.competitor_id or f"competitor:{p.competitor_id}" not in b.nodes:
            continue
        pid = b.node("patent", p.id, p.title, attrs={"domain": p.tech_domain_id},
                     provenance=p.provenance)
        b.edge(f"competitor:{p.competitor_id}", pid, "filed", provenance=p.provenance)

    for n in b.nodes.values():
        db.add(n)
    db.flush()
    n_edges = db.query(KgEdge).count()
    return {"nodes": len(b.nodes), "edges": n_edges}
