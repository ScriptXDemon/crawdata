"""Processing for the crawler-fed domains beyond signals/tenders: partnerships, geo, innovation.

Each reads a ``stg_*`` row, resolves the competitor, applies the "vs KSSL" tagging/enrichment, and
publishes a ``srv_*`` row.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.reference import RefCompetitor
from ..models.serving import SrvGeoEntry, SrvInnovation, SrvPartnership
from ..models.staging import StgDocument, StgGeo, StgInnovation, StgPartnership
from .entity_resolution import resolve_competitor
from .llm import LLMProvider

ANCHOR = "KSSL"


def _upsert(db: Session, model, key: dict, fields: dict) -> None:
    """Publish by natural key: the same real-world fact reported by another document
    updates the existing row instead of duplicating it (S-18/S-19 idempotency contract)."""
    stmt = select(model)
    for col, val in key.items():
        stmt = stmt.where(getattr(model, col) == val)
    row = db.scalars(stmt).first()
    if row is None:
        db.add(model(**key, **fields))
    else:
        for col, val in fields.items():
            setattr(row, col, val)


def _competitor_name(db: Session, cid: str | None) -> str | None:
    if not cid:
        return None
    comp = db.get(RefCompetitor, cid)
    return comp.name if comp else cid


def _doc_url(db: Session, document_id: str) -> str | None:
    doc = db.get(StgDocument, document_id)
    return doc.url if doc else None


def process_partnership(db: Session, _llm: LLMProvider, sp: StgPartnership) -> None:
    cid = resolve_competitor(db, sp.competitor_id, f"{sp.competitor_id} {sp.description or ''}")
    name = _competitor_name(db, cid) or sp.competitor_id
    lines = sp.detected_lines or []
    relevance = "CORE" if lines else ("ADJACENT" if sp.partner_kind else "context")

    line_txt = ", ".join(lines) if lines else "non-core"
    dep = (
        f"{sp.partner_name} ({sp.partner_country}) holds the IP"
        if sp.partner_kind == "Foreign OEM"
        else f"Domestic tie ({sp.partner_name}) — lower sanctions risk"
    )
    meaning = (
        f"<b>Threat:</b> {name} gains {line_txt} capability via {sp.partner_name}. "
        f"<b>Opening:</b> the {ANCHOR} differentiators remain indigenous IP, forging scale and "
        f"trials-maturity. <b>Dependency:</b> {dep}."
    )

    _upsert(
        db, SrvPartnership,
        key=dict(competitor_id=cid, partner_name=sp.partner_name, rel_type=sp.rel_type),
        fields=dict(
            competitor_name=name, partner_kind=sp.partner_kind, country=sp.partner_country,
            deal_value=sp.deal_value_raw, date_announced=sp.date_announced,
            kssl_relevance=relevance, meaning=meaning, provenance="sourced",
            source_url=_doc_url(db, sp.document_id),
        ),
    )
    sp.kssl_relevance = relevance
    sp.proc_status = "published"


def process_geo(db: Session, _llm: LLMProvider, sg: StgGeo) -> None:
    cid = resolve_competitor(db, sg.competitor_id, sg.competitor_id or "")
    _upsert(
        db, SrvGeoEntry,
        key=dict(competitor_id=cid, country=sg.country, product_name=sg.product_name),
        fields=dict(
            competitor_name=_competitor_name(db, cid) or sg.competitor_id,
            category=sg.product_category, contract_value=sg.contract_value_raw,
            since_year=sg.since_year, qty=sg.qty_raw, stage=sg.stage, note=sg.note,
            provenance="sourced" if sg.confidence in ("high", "medium") else "estimate",
            source_url=_doc_url(db, sg.document_id),
        ),
    )
    sg.proc_status = "published"


def process_innovation(db: Session, _llm: LLMProvider, si: StgInnovation) -> None:
    gap = "behind" if si.competitor_id else "parity"
    impact = (
        f"If fielded on {si.horizon_hint or 'the stated horizon'}, this shifts the {si.tech_domain} "
        f"benchmark {ANCHOR} is measured against."
    )
    action = f"Assess {ANCHOR}'s roadmap in {si.tech_domain} against this development and brief R&D."
    _upsert(
        db, SrvInnovation,
        key=dict(tech_domain_id=si.tech_domain, title=si.title),
        fields=dict(
            maturity=si.maturity_hint, gap_vs_kssl=gap, driver=si.driver,
            horizon=si.horizon_hint, body=si.description, impact=impact, action=action,
            provenance="sourced", source_url=_doc_url(db, si.document_id),
        ),
    )
    si.proc_status = "published"
