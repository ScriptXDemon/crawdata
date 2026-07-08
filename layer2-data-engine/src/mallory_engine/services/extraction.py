"""Document → typed-record extraction — the missing link between L1 and the pipeline.

The crawler sends ONE bare page bundle per kept page ("L1 sends exactly one bundle; L2 does
its own deep processing"). This stage derives the typed staging records from a stored
document.

Two extractors, LLM-primary / regex-fallback:
  * ``extract_document`` asks the fast LLM (``llm.extract_records``) to read the page and
    return typed records. On empty/invalid output (or stub/offline) it falls back to
    ``_regex_extract_document`` — the original deterministic keyword+pattern extractor —
    so ingestion NEVER depends on a model being up. Stub mode ⇒ regex always ⇒ output is
    byte-identical to before.
  * The LLM calls are fanned out over a thread pool (httpx is threadsafe, Ollama serves
    parallel); ALL db writes stay on the calling thread to avoid session threading issues.

Idempotent: a document is extracted once (``extracted_at``); documents that arrived WITH
crawler-supplied records (mock feeder, tests) are stamped and skipped.
"""

from __future__ import annotations

import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.reference import RefCompetitor
from ..models.staging import (
    StgCompanyEvent,
    StgDocument,
    StgGeo,
    StgPartnership,
    StgSignal,
    StgTender,
)

_TENDER_PAT = re.compile(
    r"\b(tender|rfp|rfi|request for proposal|solicitation|procurement of|invites? bids?|eoi)\b",
    re.IGNORECASE)
_PARTNER_PAT = re.compile(
    r"\b(licen[cs]\w*|joint venture|jv\b|mou\b|teaming|partnership|technology partner\w*|"
    r"agreement with)\b", re.IGNORECASE)
_ACQ_PAT = re.compile(r"\b(acquires?|acquisition of|buys|takes? .{0,12}stake in)\b", re.IGNORECASE)
_GEO_PAT = re.compile(r"\b(export|order|contract|deliver\w*|wins?|supplie[sd])\b", re.IGNORECASE)
_VALUE_PAT = re.compile(
    r"((?:₹|Rs\.?|\$|€|£)\s?[\d,]+(?:\.\d+)?\s?(?:cr|crore|lakh|bn|billion|mn|million|k)?)",
    re.IGNORECASE)
_DEADLINE_PAT = re.compile(r"closing in (\d{1,3}) days", re.IGNORECASE)

_REL_TYPE = [("licen", "license"), ("joint venture", "jv"), ("jv", "jv"), ("mou", "mou"),
             ("invest", "investment"), ("suppl", "supply")]

# Small keyword → category map for tender category hints (extensible via ref data later).
_CATEGORY_KEYWORDS = {
    "artillery": ("155mm", "howitzer", "artillery", "gun system", "mounted gun"),
    "uav": ("drone", "uav", "loitering", "unmanned"),
    "ammunition": ("ammunition", "munition", "shell", "propellant"),
    "small_arms": ("rifle", "carbine", "small arms", "pistol"),
    "missiles_ad": ("missile", "air defence", "air defense"),
}


def _entities(doc: StgDocument) -> dict[str, list[dict]]:
    by_type: dict[str, list[dict]] = {}
    for e in doc.entities_detected or []:
        by_type.setdefault(e.get("type") or "unknown", []).append(e)
    return by_type


def _category_hint(text: str) -> str | None:
    low = text.lower()
    for cat, keys in _CATEGORY_KEYWORDS.items():
        if any(k in low for k in keys):
            return cat
    return None


def _tech_domain(db: Session, text: str) -> str | None:
    """Match ref_tech_domains keywords against the text (the crawler's seed vocabulary)."""
    from ..models.reference import RefTechDomain
    low = text.lower()
    for d in db.scalars(select(RefTechDomain)).all():
        if any((k or "").lower() in low for k in (d.keywords or [])):
            return d.id
    return None


def _regex_extract_document(db: Session, doc: StgDocument) -> dict[str, int]:
    """Deterministic keyword/pattern extraction (the fallback path). Returns counts."""
    text = f"{doc.title or ''}. {doc.main_text_en or doc.main_text or ''}"
    ents = _entities(doc)
    competitor = next((e.get("resolved_id") for e in ents.get("competitor", [])
                       if e.get("resolved_id")), None)
    country = next((e.get("resolved_id") or e.get("surface")
                    for e in ents.get("country", [])), None)
    products = [e.get("resolved_id") or e.get("surface") for e in ents.get("product", [])]
    partner = next((e.get("surface") for e in
                    ents.get("partner", []) + ents.get("unknown_company", [])), None)

    value_m = _VALUE_PAT.search(text)
    value_raw = value_m.group(1).strip() if value_m else None
    is_tender = bool(_TENDER_PAT.search(text))
    counts = {"signals": 0, "tenders": 0, "partnerships": 0, "geo": 0, "events": 0}

    # ── signal: every kept page is one (the crawler already gated relevance) ──
    stream = ("market" if is_tender
              else "technology" if not competitor and _tech_domain(db, text)
              else "competitive")
    db.add(StgSignal(
        document_id=doc.id, stream=stream, competitor_id=competitor,
        detected_products=products or None, detected_country=country,
        tech_domain=_tech_domain(db, text), event_summary=doc.title,
        deal_value_raw=value_raw, published_at=doc.published_at, proc_status="received",
    ))
    counts["signals"] = 1

    # ── tender ──
    if is_tender:
        dl = _DEADLINE_PAT.search(text)
        deadline = (dt.date.today() + dt.timedelta(days=int(dl.group(1)))) if dl else None
        db.add(StgTender(
            document_id=doc.id, title=doc.title, issuer=doc.source_id, country=country,
            category_hint=_category_hint(text), value_raw=value_raw,
            deadline_date=deadline, requirement_text=(doc.summary or doc.main_text or "")[:500],
            requirement_fields=[
                {"label": r.get("label", ""), "value": r.get("value", "")}
                for t in (doc.tables or []) for r in t.get("rows", [])
                if isinstance(r, dict) and r.get("label")
            ],
            proc_status="received",
        ))
        counts["tenders"] = 1

    # ── partnership ──
    m = _PARTNER_PAT.search(text)
    if m and competitor and partner:
        rel = next((v for k, v in _REL_TYPE if k in m.group(1).lower()), None)
        db.add(StgPartnership(
            document_id=doc.id, competitor_id=competitor, partner_name=partner,
            rel_type=rel, deal_value_raw=value_raw,
            date_announced=doc.published_at.date() if doc.published_at else None,
            description=doc.title, proc_status="received",
        ))
        counts["partnerships"] = 1

    # ── geo footprint ──
    if competitor and country and _GEO_PAT.search(text):
        low = text.lower()
        stage = "Contracted" if any(w in low for w in ("order", "contract", "win")) else "Offered"
        db.add(StgGeo(
            document_id=doc.id, competitor_id=competitor, country=country,
            product_name=products[0] if products else None,
            contract_value_raw=value_raw, stage=stage, confidence="medium",
            proc_status="received",
        ))
        counts["geo"] = 1

    # ── company event (acquisition) ──
    if competitor and _ACQ_PAT.search(text):
        db.add(StgCompanyEvent(
            document_id=doc.id, competitor_id=competitor, event_type="acquisition",
            headline=doc.title, deal_value_raw=value_raw,
            date_of_event=doc.published_at.date() if doc.published_at else None,
            description=doc.summary, proc_status="received",
        ))
        counts["events"] = 1

    return counts


# ── LLM-primary path ──────────────────────────────────────────────────────────

_VALID_STREAMS = {"competitive", "market", "technology"}


def _apply_llm_records(db: Session, doc: StgDocument, out: dict,
                       known_ids: set[str]) -> dict[str, int]:
    """Map an ``ExtractOut`` dict onto Stg* rows — same fields the regex path writes.

    Returns counts, or an empty dict if the payload has no usable signal (caller then
    falls back to regex). competitor_id is trusted only when in ``known_ids``.
    """
    sig = out.get("signal") or {}
    stream = sig.get("stream")
    summary = (sig.get("summary") or doc.title or "").strip()
    if stream not in _VALID_STREAMS or not summary:
        return {}  # unusable → let the caller fall back to regex

    def _cid(rec: dict) -> str | None:
        cid = rec.get("competitor_id")
        return cid if cid in known_ids else None

    counts = {"signals": 0, "tenders": 0, "partnerships": 0, "geo": 0, "events": 0}
    db.add(StgSignal(
        document_id=doc.id, stream=stream, competitor_id=_cid(sig),
        detected_products=sig.get("products") or None, detected_country=sig.get("country"),
        tech_domain=sig.get("tech_domain"), event_summary=summary,
        deal_value_raw=sig.get("deal_value"), published_at=doc.published_at,
        proc_status="received",
    ))
    counts["signals"] = 1

    if t := out.get("tender"):
        dl = t.get("deadline_days")
        # bound it: an LLM can emit a giant/negative day count → timedelta(days=huge) overflows.
        deadline = (dt.date.today() + dt.timedelta(days=int(dl))
                    if isinstance(dl, int) and 0 <= dl <= 3650 else None)
        db.add(StgTender(
            document_id=doc.id, title=(t.get("title") or summary), issuer=doc.source_id,
            country=t.get("country"), category_hint=t.get("category"), value_raw=t.get("value"),
            deadline_date=deadline,
            requirement_text=(doc.summary or doc.main_text or "")[:500],
            requirement_fields=_table_fields(doc), proc_status="received",
        ))
        counts["tenders"] = 1

    if (p := out.get("partnership")) and p.get("partner_name") and _cid(p):
        db.add(StgPartnership(
            document_id=doc.id, competitor_id=_cid(p), partner_name=p["partner_name"],
            rel_type=p.get("rel_type"), deal_value_raw=p.get("value"),
            date_announced=doc.published_at.date() if doc.published_at else None,
            description=doc.title, proc_status="received",
        ))
        counts["partnerships"] = 1

    if (g := out.get("geo")) and g.get("country") and _cid(g):
        db.add(StgGeo(
            document_id=doc.id, competitor_id=_cid(g), country=g["country"],
            product_name=g.get("product"), contract_value_raw=g.get("value"),
            stage=g.get("stage") or "Offered", confidence="medium", proc_status="received",
        ))
        counts["geo"] = 1

    if (e := out.get("event")) and e.get("headline") and _cid(e):
        db.add(StgCompanyEvent(
            document_id=doc.id, competitor_id=_cid(e),
            event_type=e.get("event_type") or "event", headline=e["headline"],
            deal_value_raw=e.get("value"),
            date_of_event=doc.published_at.date() if doc.published_at else None,
            description=doc.summary, proc_status="received",
        ))
        counts["events"] = 1

    return counts


def _table_fields(doc: StgDocument) -> list[dict]:
    return [
        {"label": r.get("label", ""), "value": r.get("value", "")}
        for t in (doc.tables or []) for r in t.get("rows", [])
        if isinstance(r, dict) and r.get("label")
    ]


def extract_document(db: Session, doc: StgDocument, llm, known_ids: set[str]) -> dict[str, int]:
    """LLM-primary extraction with regex fallback. Stamps extracted_at. Returns counts.

    The LLM call is expected to have already run (fanned out in extract_pending); pass its
    result via ``doc`` context is avoided — instead extract_pending precomputes and calls
    ``_apply_llm_records`` directly. This wrapper is kept for single-doc/test use.
    """
    out = llm.extract_records(
        title=doc.title or "", text=doc.main_text_en or doc.main_text or "",
        entities_detected=doc.entities_detected or [], tables=doc.tables or [],
    ) if llm is not None else {}
    counts = _apply_llm_records(db, doc, out, known_ids) if out else {}
    if not counts:  # LLM empty/invalid/offline → deterministic regex fallback
        counts = _regex_extract_document(db, doc)
    doc.extracted_at = dt.datetime.now(dt.timezone.utc)
    return counts


def extract_pending(db: Session, llm=None) -> dict[str, int]:
    """Extract every un-extracted document that arrived without crawler-supplied records.

    LLM extraction is fanned out over a thread pool (I/O-bound HTTP); all DB writes happen
    serially on this thread afterwards. ``llm=None`` (or stub) ⇒ pure regex, byte-identical
    to the pre-LLM behaviour.
    """
    totals = {"docs": 0, "signals": 0, "tenders": 0, "partnerships": 0, "geo": 0, "events": 0}
    docs = db.scalars(select(StgDocument).where(StgDocument.extracted_at.is_(None))).all()

    pending: list[StgDocument] = []
    for doc in docs:
        has_children = db.scalar(
            select(StgSignal.id).where(StgSignal.document_id == doc.id).limit(1)
        ) is not None or db.scalar(
            select(StgTender.id).where(StgTender.document_id == doc.id).limit(1)
        ) is not None
        if has_children:  # records were supplied at ingest — nothing to derive
            doc.extracted_at = dt.datetime.now(dt.timezone.utc)
            continue
        pending.append(doc)

    known_ids = {c.id for c in db.scalars(select(RefCompetitor)).all()}

    # Fan out the LLM extraction (pure HTTP → dict). Workers MUST NOT touch the request
    # session: a SQLAlchemy Session is not thread-safe, and the db-bound llm's cache/ledger
    # writes would deadlock it under 4 concurrent threads. So use a DB-LESS llm here (no
    # cache/ledger during the fan-out); the pipeline's db-bound llm still logs the serial tasks.
    outputs: dict[str, dict] = {}
    worker_llm = llm.with_db(None) if hasattr(llm, "with_db") else llm
    if worker_llm is not None and pending:
        def _call(doc: StgDocument) -> tuple[str, dict]:
            try:
                return doc.id, worker_llm.extract_records(
                    title=doc.title or "", text=doc.main_text_en or doc.main_text or "",
                    entities_detected=doc.entities_detected or [], tables=doc.tables or [],
                )
            except Exception:
                return doc.id, {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for doc_id, out in pool.map(_call, pending):
                outputs[doc_id] = out

    # Apply serially on this thread (LLM records → fallback to regex when empty).
    for doc in pending:
        out = outputs.get(doc.id) or {}
        counts = _apply_llm_records(db, doc, out, known_ids) if out else {}
        if not counts:
            counts = _regex_extract_document(db, doc)
        doc.extracted_at = dt.datetime.now(dt.timezone.utc)
        totals["docs"] += 1
        for k, v in counts.items():
            totals[k] += v
    return totals
