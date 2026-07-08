"""Interface A — the Ingest API (L1 → L2).

The crawler POSTs a page envelope (one document + N typed records) or individual records. Every
body is validated against the Pydantic contract, so a malformed record is rejected with HTTP 422
before it reaches staging. Writes ``stg_*`` with ``proc_status='received'``; processing is a
separate step (see ``ops.process``), keeping ingestion and compute cleanly decoupled.
"""

from __future__ import annotations

import datetime as dt
import hashlib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts.ingest import (
    CompanyEventIn,
    CompetitiveSignalIn,
    DocumentIn,
    GeoFootprintIn,
    InnovationIn,
    PageEnvelopeIn,
    PartnershipIn,
    TenderIn,
)
from ..db import get_db
from ..models import staging as stg

router = APIRouter(prefix="/ingest/v1", tags=["ingest"])


def _doc_id(url: str) -> str:
    return "doc_" + hashlib.sha1(url.encode()).hexdigest()[:12]


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def upsert_document(db: Session, d: DocumentIn) -> str:
    did = _doc_id(d.url)
    fields = dict(
        url=d.url, content_hash=d.content_hash, source_id=d.source_id, source_tier=d.source_tier,
        title=d.title, author=d.author, published_at=d.published_at, date_precision=d.date_precision,
        language=d.language, access=d.access, main_text=d.main_text, main_text_en=d.main_text_en,
        summary=d.summary, images=[i.model_dump(mode="json") for i in d.images],
        attachments=[a.model_dump(mode="json") for a in d.attachments],
        screenshot=d.screenshot.model_dump(mode="json") if d.screenshot else None,
        tables=[t.model_dump(mode="json") for t in d.tables],
        entities_detected=[e.model_dump(mode="json") for e in d.entities_detected],
        fetched_at=d.fetched_at,
    )
    existing = db.get(stg.StgDocument, did)
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        db.add(stg.StgDocument(id=did, received_at=_now(), dedup_status="new", **fields))
    db.flush()  # ensure the document row exists before child records reference it
    return did


def _ingest_signal(db: Session, did: str, r: CompetitiveSignalIn) -> bool:
    dup = db.scalar(
        select(stg.StgSignal).where(
            stg.StgSignal.document_id == did, stg.StgSignal.event_summary == r.event_summary
        )
    )
    if dup:
        return False
    db.add(stg.StgSignal(
        document_id=did, stream=r.stream, competitor_id=r.competitor_id,
        detected_products=r.detected_products, detected_country=r.detected_country,
        tech_domain=r.tech_domain, event_summary=r.event_summary, deal_value_raw=r.deal_value_raw,
        deal_value_num=r.deal_value_num, deal_currency=r.deal_currency, published_at=r.published_at,
    ))
    return True


def _ingest_tender(db: Session, did: str, r: TenderIn) -> bool:
    key = r.source_ref or r.title
    dup = db.scalar(
        select(stg.StgTender).where(
            stg.StgTender.document_id == did,
            (stg.StgTender.source_ref == key) | (stg.StgTender.title == r.title),
        )
    )
    if dup:
        return False
    db.add(stg.StgTender(
        document_id=did, source_ref=r.source_ref, title=r.title, issuer=r.issuer, country=r.country,
        category_hint=r.category_hint, value_raw=r.value_raw, value_num=r.value_num,
        value_currency=r.value_currency, qty_raw=r.qty_raw, deadline_date=r.deadline_date,
        requirement_text=r.requirement_text,
        requirement_fields=[f.model_dump(mode="json") for f in r.requirement_fields],
    ))
    return True


def _exists(db: Session, model, **filters) -> bool:
    stmt = select(model)
    for col, val in filters.items():
        stmt = stmt.where(getattr(model, col) == val)
    return db.scalar(stmt) is not None


def _ingest_partnership(db: Session, did: str, r: PartnershipIn) -> None:
    if _exists(db, stg.StgPartnership, document_id=did, partner_name=r.partner_name):
        return
    db.add(stg.StgPartnership(document_id=did, **r.model_dump(exclude={"document_id"})))


def _ingest_geo(db: Session, did: str, r: GeoFootprintIn) -> None:
    if _exists(db, stg.StgGeo, document_id=did, product_name=r.product_name, country=r.country):
        return
    db.add(stg.StgGeo(document_id=did, **r.model_dump(exclude={"document_id"})))


def _ingest_innovation(db: Session, did: str, r: InnovationIn) -> None:
    if _exists(db, stg.StgInnovation, document_id=did, title=r.title):
        return
    db.add(stg.StgInnovation(document_id=did, **r.model_dump(exclude={"document_id"})))


def _ingest_company_event(db: Session, did: str, r: CompanyEventIn) -> None:
    if _exists(db, stg.StgCompanyEvent, document_id=did, headline=r.headline):
        return
    db.add(stg.StgCompanyEvent(document_id=did, **r.model_dump(exclude={"document_id"})))


@router.post("/page", summary="Ingest one page (document + records) atomically")
def ingest_page(payload: PageEnvelopeIn, db: Session = Depends(get_db)) -> dict:
    did = upsert_document(db, payload.document)
    counts = {
        "signals": sum(_ingest_signal(db, did, r) for r in payload.signals),
        "tenders": sum(_ingest_tender(db, did, r) for r in payload.tenders),
    }
    for r in payload.partnerships:
        _ingest_partnership(db, did, r)
    for r in payload.geo:
        _ingest_geo(db, did, r)
    for r in payload.innovation:
        _ingest_innovation(db, did, r)
    for r in payload.company_events:
        _ingest_company_event(db, did, r)
    db.commit()
    return {"document_id": did, "ingested": counts}


@router.post("/document", summary="Upsert a document only")
def ingest_document(payload: DocumentIn, db: Session = Depends(get_db)) -> dict:
    did = upsert_document(db, payload)
    db.commit()
    return {"document_id": did}


# Per-record bundle ingestion — matches the crawler's POST /ingest/v1/{record_type} with
# body {document, record}. This is the shape the Layer 1 crawler forwards.
_DISPATCH: dict[str, tuple[type, object]] = {
    "competitive_signal": (CompetitiveSignalIn, _ingest_signal),
    "tender": (TenderIn, _ingest_tender),
    "partnership": (PartnershipIn, _ingest_partnership),
    "geo_footprint": (GeoFootprintIn, _ingest_geo),
    "innovation": (InnovationIn, _ingest_innovation),
    "company_event": (CompanyEventIn, _ingest_company_event),
}


@router.post("/{record_type}", summary="Ingest one {document, record} bundle (crawler forward shape)")
def ingest_bundle(record_type: str, body: dict, db: Session = Depends(get_db)) -> dict:
    if record_type not in _DISPATCH:
        raise HTTPException(422, {"failing_rule": "unknown_record_type"})
    doc_raw = body.get("document") or {}
    if not (doc_raw.get("main_text") or "").strip():
        raise HTTPException(422, {"failing_rule": "rule1_empty_main_text"})
    try:
        document = DocumentIn.model_validate(doc_raw)
        model, fn = _DISPATCH[record_type]
        record = model.model_validate(body.get("record") or {})
    except ValidationError as e:
        raise HTTPException(422, {"failing_rule": "rule3_invalid_record", "detail": e.errors()})
    did = upsert_document(db, document)
    fn(db, did, record)  # type: ignore[operator]
    db.commit()
    return {"accepted": True, "document_id": did}
