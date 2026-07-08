"""Extraction (bare document → typed records) + publish idempotency — the continuous spine."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from mallory_engine.models.reference import RefCompetitor
from mallory_engine.models.serving import SrvPartnership
from mallory_engine.models.staging import (
    StgCompanyEvent,
    StgDocument,
    StgGeo,
    StgPartnership,
    StgSignal,
    StgTender,
)
from mallory_engine.pipeline import runner
from mallory_engine.services import extraction
from mallory_engine.services.llm.stub import StubLLMProvider

FRESH = dt.datetime(2026, 7, 7, tzinfo=dt.timezone.utc)


def _doc(db: Session, doc_id: str, title: str, text: str, *, entities=None,
         source="IDRW", tier=3) -> None:
    db.add(StgDocument(
        id=doc_id, url=f"https://x.test/{doc_id}", content_hash=f"h:{doc_id}",
        source_id=source, source_tier=tier, title=title, main_text=text,
        entities_detected=entities or [], published_at=FRESH, received_at=FRESH,
    ))


def test_bare_document_becomes_signal_with_resolved_entities(db: Session) -> None:
    db.add(RefCompetitor(id="LT", name="L&T", is_anchor=False))
    _doc(db, "d1", "L&T secures Rs 4,500 cr K9 Vajra follow-on order",
         "The MoD awarded L&T a follow-on contract worth Rs 4,500 cr.",
         entities=[{"surface": "L&T", "resolved_id": "LT", "type": "competitor"},
                   {"surface": "India", "resolved_id": "IN", "type": "country"}])
    db.commit()

    totals = extraction.extract_pending(db)
    db.commit()
    assert totals == {"docs": 1, "signals": 1, "tenders": 0, "partnerships": 0,
                      "geo": 1, "events": 0}
    sig = db.scalars(select(StgSignal)).one()
    assert sig.competitor_id == "LT"
    assert sig.stream == "competitive"
    assert sig.deal_value_raw and "4,500" in sig.deal_value_raw
    # order + country → geo footprint too
    geo = db.scalars(select(StgGeo)).one()
    assert geo.stage == "Contracted"


def test_tender_document_yields_tender_record(db: Session) -> None:
    _doc(db, "d2", "MoD issues RFP for 155mm Mounted Gun System",
         "Request for proposal. Range at least 45 km. Closing in 40 days.",
         source="MOD_IN", tier=1)
    db.commit()
    totals = extraction.extract_pending(db)
    assert totals["tenders"] == 1
    t = db.scalars(select(StgTender)).one()
    assert t.category_hint == "artillery"
    assert t.deadline_date == dt.date.today() + dt.timedelta(days=40)
    sig = db.scalars(select(StgSignal)).one()
    assert sig.stream == "market"


def test_partnership_and_acquisition_patterns(db: Session) -> None:
    db.add(RefCompetitor(id="NIBE", name="NIBE", is_anchor=False))
    _doc(db, "d3", "NIBE signs licensing agreement with Sig Sauer",
         "NIBE Ltd signed a licensing agreement with Sig Sauer.",
         entities=[{"surface": "NIBE", "resolved_id": "NIBE", "type": "competitor"},
                   {"surface": "Sig Sauer", "type": "unknown_company"}])
    db.add(RefCompetitor(id="ADANI", name="Adani", is_anchor=False))
    _doc(db, "d4", "Adani acquires General Aeronautics",
         "Adani Defence announced the acquisition of drone maker General Aeronautics.",
         entities=[{"surface": "Adani", "resolved_id": "ADANI", "type": "competitor"},
                   {"surface": "General Aeronautics", "type": "unknown_company"}])
    db.commit()
    totals = extraction.extract_pending(db)
    assert totals["partnerships"] == 1 and totals["events"] == 1
    p = db.scalars(select(StgPartnership)).one()
    assert (p.competitor_id, p.partner_name, p.rel_type) == ("NIBE", "Sig Sauer", "license")
    ev = db.scalars(select(StgCompanyEvent)).one()
    assert ev.event_type == "acquisition"


def test_extraction_is_idempotent_and_skips_docs_with_supplied_records(db: Session) -> None:
    _doc(db, "d5", "Some event happened", "Body text.")
    db.add(StgDocument(id="d6", url="https://x.test/d6", content_hash="h:d6", source_id="X",
                       title="Doc with supplied record", main_text="b", received_at=FRESH))
    db.add(StgSignal(document_id="d6", stream="competitive",
                     event_summary="supplied", proc_status="received"))
    db.commit()

    t1 = extraction.extract_pending(db)
    db.commit()
    t2 = extraction.extract_pending(db)
    assert t1["docs"] == 1  # d6 skipped (has supplied records), d5 extracted
    assert t2["docs"] == 0  # second run: nothing left
    assert db.query(StgSignal).count() == 2  # d5's derived + d6's supplied, no dupes


def test_same_partnership_via_two_documents_publishes_one_row(db: Session) -> None:
    """The idempotent-publish fix: one fact, two source documents, one srv row."""
    db.add(RefCompetitor(id="NIBE", name="NIBE", is_anchor=False))
    for i, src in enumerate(["IDRW", "JANES"], start=1):
        _doc(db, f"p{i}", f"NIBE signs licensing agreement with Sig Sauer ({src})",
             "Licensing agreement signed.", source=src)
        db.add(StgPartnership(document_id=f"p{i}", competitor_id="NIBE",
                              partner_name="Sig Sauer", rel_type="license",
                              proc_status="received"))
    db.commit()

    runner.process_pending(db, StubLLMProvider())
    rows = db.scalars(select(SrvPartnership).where(
        SrvPartnership.partner_name == "Sig Sauer")).all()
    assert len(rows) == 1, "same fact from two documents must upsert, not duplicate"
