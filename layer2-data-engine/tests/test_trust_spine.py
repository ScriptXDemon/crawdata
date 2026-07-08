"""End-to-end trust spine on in-memory SQLite: corroboration → confidence → evidence chain.

The Janes-style claim: two independent sources reporting the same award must outrank a single
source, and every published card must carry a traceable evidence link.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from mallory_engine import models  # noqa: F401  (registers all tables)
from mallory_engine.db import Base


# Legacy models use Postgres-only JSONB directly. For the in-memory SQLite test engine, render
# it as plain JSON so create_all() succeeds — production runs on Postgres where JSONB is native.
@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


from mallory_engine.models.serving import SrvEvidence, SrvSignal  # noqa: E402
from mallory_engine.models.staging import StgDocument, StgSignal  # noqa: E402
from mallory_engine.pipeline import runner  # noqa: E402
from mallory_engine.services.llm.stub import StubLLMProvider  # noqa: E402

FRESH = dt.datetime(2026, 7, 5, tzinfo=dt.timezone.utc)


@pytest.fixture
def db() -> Session:
    # StaticPool + check_same_thread=False: one shared connection so FastAPI's TestClient
    # (different thread) sees the same in-memory DB.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _doc(s: Session, doc_id: str, source_id: str, tier: int) -> None:
    s.add(StgDocument(
        id=doc_id, url=f"https://{source_id.lower()}.test/{doc_id}", content_hash=f"h:{doc_id}",
        source_id=source_id, source_tier=tier, title="L&T secures Rs 4,500 cr K9 Vajra order",
        main_text="L&T won the K9 Vajra follow-on.", published_at=FRESH,
        received_at=FRESH,
    ))


def _sig(s: Session, sig_id: int, doc_id: str) -> None:
    s.add(StgSignal(
        id=sig_id, document_id=doc_id, stream="competitive", competitor_id="LT",
        detected_country="IN", event_summary="L&T secures Rs 4,500 cr K9 Vajra follow-on order",
        deal_value_raw="₹4,500 cr", deal_value_num=4500.0, published_at=FRESH,
        proc_status="received",
    ))


def test_two_independent_sources_outrank_one(db: Session) -> None:
    # Same award reported by IDRW (tier 3) and Janes (tier 1) → corroboration should be 2.
    _doc(db, "doc_a", "IDRW", 3)
    _doc(db, "doc_b", "JANES", 1)
    _sig(db, 1, "doc_a")
    _sig(db, 2, "doc_b")
    # A different, single-source event for contrast.
    _doc(db, "doc_c", "IDRW", 3)
    db.add(StgSignal(
        id=3, document_id="doc_c", stream="competitive", competitor_id="ADANI",
        detected_country="AE", event_summary="Adani opens ammunition line in UAE",
        deal_value_raw="₹21,000 cr", deal_value_num=21000.0, published_at=FRESH,
        proc_status="received",
    ))
    db.commit()

    runner.process_pending(db, StubLLMProvider())

    corroborated = db.get(SrvSignal, 2)   # the Janes report of the shared award
    lone = db.get(SrvSignal, 3)

    assert corroborated.corroboration == 2, "two sources for one award"
    assert lone.corroboration == 1
    assert corroborated.confidence is not None
    # tier-1 + corroboration beats a lone tier-3 aggregator
    assert corroborated.confidence > lone.confidence
    assert corroborated.confidence_band in {"high", "medium", "low"}
    assert corroborated.confidence_parts and len(corroborated.confidence_parts) == 4


def test_every_card_has_an_evidence_link(db: Session) -> None:
    _doc(db, "doc_a", "IDRW", 3)
    _sig(db, 1, "doc_a")
    db.commit()
    runner.process_pending(db, StubLLMProvider())

    ev = db.scalars(
        select(SrvEvidence).where(
            SrvEvidence.target_kind == "signal", SrvEvidence.target_id == "1"
        )
    ).all()
    assert len(ev) >= 1
    assert ev[0].evidence_id == "doc:doc_a"
    assert ev[0].source_url == "https://idrw.test/doc_a"
    assert ev[0].method == "rule"


def test_recompute_ranks_handles_mixed_tz_datetimes(db: Session) -> None:
    """SQLite returns naive datetimes; the ranker must sort them alongside aware ones."""
    from mallory_engine.models.serving import SrvSignal
    from mallory_engine.services import signal_pipeline
    naive = dt.datetime(2026, 7, 1, 12, 0)  # no tzinfo — what SQLite hands back
    aware = dt.datetime(2026, 7, 5, 12, 0, tzinfo=dt.timezone.utc)
    db.add(SrvSignal(id=10, pillar="competitive", dir="threat", rank=999, title="a",
                     published_at=naive))
    db.add(SrvSignal(id=11, pillar="competitive", dir="watch", rank=999, title="b",
                     published_at=aware))
    db.add(SrvSignal(id=12, pillar="competitive", dir="threat", rank=999, title="c",
                     published_at=None))
    db.commit()
    signal_pipeline.recompute_ranks(db)  # must not raise
    ranks = {s.id: s.rank for s in db.scalars(select(SrvSignal)).all()}
    assert ranks[10] == 1  # threat + a real date outranks the null-date threat and the watch


def test_explain_endpoint_returns_chain(db: Session) -> None:
    from fastapi.testclient import TestClient
    from mallory_engine.api.serving import router
    from mallory_engine.db import get_db
    from fastapi import FastAPI

    _doc(db, "doc_a", "JANES", 1)
    _sig(db, 1, "doc_a")
    db.commit()
    runner.process_pending(db, StubLLMProvider())

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)

    r = client.get("/api/v1/explain/signal/1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target_kind"] == "signal"
    assert body["confidence"] is not None
    assert body["confidence_band"] in {"high", "medium", "low"}
    assert body["evidence_count"] >= 1
    assert body["fields"][0]["evidence"][0]["eid"] == "doc:doc_a"
    assert body["fields"][0]["evidence"][0]["source_tier"] == 1
