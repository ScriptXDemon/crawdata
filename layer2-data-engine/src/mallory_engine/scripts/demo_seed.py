"""Seed a live demo database: reference data + real crawled events -> full pipeline.

    # SQLite, no docker (from layer2-data-engine/):
    DATABASE_URL=sqlite:///./mallory_demo.db LLM_PROVIDER=ollama \
        python -m mallory_engine.scripts.demo_seed

Idempotent-ish: drops and recreates all tables, so re-running gives a fresh demo state.
The events mirror cralwer/data/output/ingested.ndjson — the crawler's real catches.
"""

from __future__ import annotations

import datetime as dt
import sys

from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import Base, engine
from ..pipeline import runner
from ..seed import loader
from ..services import competitor_synthesis, field_patterns
from ..services.llm import get_llm
from ..models.staging import StgDocument, StgGeo, StgPartnership, StgSignal, StgTender

NOW = dt.datetime.now(dt.timezone.utc)


def _d(days: int) -> dt.datetime:
    return NOW - dt.timedelta(days=days)


DOCS = [
    ("doc_lt1", "IDRW", 3, "L&T secures ₹4,500 cr K9 Vajra follow-on order",
     "The MoD awarded L&T a follow-on contract worth ₹4,500 cr for 100 K9 Vajra-T 155mm "
     "self-propelled howitzers. Produced at Hazira under technology partnership with Hanwha "
     "Aerospace of South Korea.", 10),
    ("doc_lt2", "JANES", 1, "India orders 100 more K9 Vajra self-propelled howitzers from L&T",
     "Follow-on order confirmed for the K9 Vajra-T programme, value approximately ₹4,500 cr.", 9),
    ("doc_ad1", "ECOTIMES", 2, "Adani Defence acquires General Aeronautics in UAV push",
     "Adani Defence and Aerospace announced the acquisition of drone maker General Aeronautics.", 12),
    ("doc_knds1", "OPEXNEWS", 3, "KNDS remporte une commande de CAESAR pour le Nigeria",
     "KNDS wins a CAESAR 6x6 155mm artillery order for Nigeria; deliveries from 2027.", 21),
    ("doc_nibe1", "DEFNEWS", 2, "NIBE signs licensing agreement with Sig Sauer",
     "NIBE Ltd signed a licensing agreement with Sig Sauer for small-arms production in India.", 18),
    ("doc_sol1", "IDRW", 3, "Solar Industries to export Nagastra loitering munitions to Armenia",
     "Solar Industries secured an export order for Nagastra-1 loitering munitions to Armenia.", 15),
    ("doc_ten1", "MOD_IN", 1, "MoD India — RFP: 155mm 52-cal Mounted Gun System",
     "RFP for a 155mm/52-calibre mounted gun system. Range at least 45 km, weight under "
     "18 tonnes. Closing in 40 days.", 6),
    ("doc_ten2", "ARMENIA_MOD", 2, "Armenia issues tender for 155mm artillery systems",
     "Armenia's MoD issued a tender for 155mm towed and mounted artillery. Closing in 30 days.", 8),
]

SIGNALS = [
    (1, "doc_lt1", "competitive", "LT", "IN",
     "L&T secures ₹4,500 cr K9 Vajra follow-on order", "₹4,500 cr", 4500.0, 10),
    (2, "doc_lt2", "competitive", "LT", "IN",
     "India orders 100 more K9 Vajra howitzers from L&T", "₹4,500 cr", 4500.0, 9),
    (3, "doc_ad1", "competitive", "ADANI", "IN",
     "Adani Defence acquires General Aeronautics in UAV push", None, None, 12),
    (4, "doc_knds1", "competitive", "KNDS", "NG",
     "KNDS wins CAESAR 6x6 order for Nigeria", None, None, 21),
    (5, "doc_sol1", "market", "SOLAR", "AM",
     "Solar Industries to export Nagastra loitering munitions to Armenia", None, None, 15),
    (6, "doc_ten1", "market", None, "IN",
     "MoD India issues RFP for 155mm 52-cal Mounted Gun System, closing in 40 days",
     None, None, 6),
    (7, "doc_nibe1", "technology", "NIBE", "IN",
     "NIBE signs licensing agreement with Sig Sauer", None, None, 18),
]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = get_settings()
    print(f"db={settings.database_url}  llm={settings.llm_provider}")

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as s:
        print("seed:", loader.load_all(s))

        for did, src, tier, title, text, age in DOCS:
            s.add(StgDocument(id=did, url=f"https://{src.lower()}.example/{did}",
                              content_hash=f"h:{did}", source_id=src, source_tier=tier,
                              title=title, main_text=text, published_at=_d(age),
                              received_at=_d(age)))
        for sid, did, stream, comp, ctry, summ, vraw, vnum, age in SIGNALS:
            s.add(StgSignal(id=sid, document_id=did, stream=stream, competitor_id=comp,
                            detected_country=ctry, event_summary=summ, deal_value_raw=vraw,
                            deal_value_num=vnum, published_at=_d(age), proc_status="received"))
        s.add(StgPartnership(id=1, document_id="doc_nibe1", competitor_id="NIBE",
                             partner_name="Sig Sauer", partner_kind="Foreign OEM",
                             rel_type="license", partner_country="USA",
                             date_announced=_d(18).date(),
                             description="Licensing agreement for small-arms production",
                             proc_status="received"))
        s.add(StgPartnership(id=2, document_id="doc_lt1", competitor_id="LT",
                             partner_name="Hanwha Aerospace", partner_kind="Foreign OEM",
                             rel_type="license", partner_country="South Korea",
                             date_announced=_d(10).date(),
                             description="K9 Vajra produced under Hanwha technology partnership",
                             proc_status="received"))
        s.add(StgGeo(id=1, document_id="doc_knds1", competitor_id="KNDS", country="Nigeria",
                     product_name="CAESAR 6x6", stage="Contracted", proc_status="received"))
        s.add(StgGeo(id=2, document_id="doc_sol1", competitor_id="SOLAR", country="Armenia",
                     product_name="Nagastra-1", stage="Contracted", proc_status="received"))
        s.add(StgTender(id=1, document_id="doc_ten1",
                        title="MoD India RFP: 155mm 52-cal Mounted Gun System",
                        issuer="MoD India", country="India", category_hint="artillery",
                        deadline_date=(NOW + dt.timedelta(days=40)).date(),
                        requirement_fields=[{"label": "System", "value": "155mm / 52-cal"},
                                            {"label": "Range", "value": "≥ 45 km"},
                                            {"label": "Weight", "value": "< 18 tonnes"}],
                        proc_status="received"))
        s.add(StgTender(id=2, document_id="doc_ten2",
                        title="Armenia tender for 155mm artillery systems",
                        issuer="Armenia MoD", country="Armenia", category_hint="artillery",
                        deadline_date=(NOW + dt.timedelta(days=30)).date(),
                        requirement_fields=[{"label": "System", "value": "155mm"}],
                        proc_status="received"))
        s.commit()

        llm = get_llm(settings, db=s)
        print("pipeline...")
        runner.process_pending(s, llm)
        print("synthesis (LT, retried — generation is sampled; fail-safe keeps rows)...")
        for attempt in range(4):
            res = competitor_synthesis.synthesize_competitor(s, llm, "LT")
            print(f"  attempt {attempt + 1}: {res}")
            if res["status"] == "sourced":
                break
        print("field patterns:", field_patterns.refresh_field_patterns(s, llm))
        s.commit()
    print("demo DB ready.")


if __name__ == "__main__":
    main()
