"""S-07/S-09/S-10 Signal pipeline — classify, enrich, publish, rank.

Takes a received ``stg_signals`` row through resolution → classification → enrichment, writes the
serving card + detail, then ranks all published cards within each pillar.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.reference import RefCompetitor
from ..models.serving import SrvSignal, SrvSignalDetail
from ..models.staging import StgDocument, StgSignal
from . import confidence as conf
from .entity_resolution import resolve_competitor
from .evidence import EvidenceItem, doc_eid, write_evidence
from .llm import LLMProvider

_DIR_WEIGHT = {"threat": 3, "watch": 2, "fav": 1}
_GROUP_LABEL = {"threat": "Priority — Threats", "watch": "Watch", "fav": "Favourable"}


def _ago(published_at: dt.datetime | None) -> str | None:
    if not published_at:
        return None
    now = dt.datetime.now(tz=published_at.tzinfo)
    days = (now - published_at).days
    if days <= 0:
        return "today"
    if days < 30:
        return f"{days}d ago"
    return published_at.strftime("%b %Y")


def process_signal(db: Session, llm: LLMProvider, ss: StgSignal,
                   corroboration: int = 1) -> None:
    doc = db.get(StgDocument, ss.document_id)
    text = (ss.event_summary or "") + " " + (doc.main_text if doc else "")

    cid = resolve_competitor(db, ss.competitor_id, text)
    comp = db.get(RefCompetitor, cid) if cid else None
    company = comp.name if comp else None
    ss.resolved_competitor_id = cid

    cls = llm.classify_signal(
        stream=ss.stream, event_summary=ss.event_summary,
        threat_level=comp.threat_level if comp else None,
    )
    ss.dir, ss.lens, ss.tags = cls["dir"], cls["lens"], cls["tags"]

    facts: list[list[str]] = []
    if company:
        facts.append(["Company", company])
    if ss.tech_domain:
        facts.append(["Domain", ss.tech_domain])
    if ss.detected_country:
        facts.append(["Country", ss.detected_country])
    if ss.deal_value_raw:
        facts.append(["Value", ss.deal_value_raw])

    enr = llm.enrich_signal(
        stream=ss.stream, event_summary=ss.event_summary, company=company,
        dir=ss.dir, facts=facts,
    )

    meta = " · ".join(
        x for x in [ss.tech_domain or None, company, ss.deal_value_raw] if x
    ) or None

    # Trust spine: deterministic confidence over source tier + corroboration + freshness.
    score, band, parts = conf.score(
        source_tier=doc.source_tier if doc else None,
        independent_sources=corroboration,
        published_at=ss.published_at,
        provenance="sourced",
        pillar=ss.stream,
    )

    db.merge(
        SrvSignal(
            id=ss.id, pillar=ss.stream, dir=ss.dir, rank=999, rank_group=_GROUP_LABEL.get(ss.dir),
            title=ss.event_summary, meta=meta, company=company, lens=ss.lens,
            sowhat=enr["sowhat"], tags=ss.tags, ago_display=_ago(ss.published_at),
            source_url=doc.url if doc else None, provenance="sourced", published_at=ss.published_at,
            confidence=score, confidence_band=band, confidence_parts=parts,
            corroboration=corroboration,
        )
    )
    db.merge(
        SrvSignalDetail(
            signal_id=ss.id, rank_display=f"{ss.stream.title()} Signal", dir=ss.dir,
            title=ss.event_summary, facts=facts, what_text=enr["what_text"],
            why_text=enr["why_text"], lens_reads=enr["lens_reads"], actions=enr["actions"],
            suggest=enr["suggest"], source_url=doc.url if doc else None,
        )
    )

    # Evidence chain: the source document backs the card (rule-produced link, uniform with LLM paths).
    if doc:
        write_evidence(
            db, target_kind="signal", target_id=ss.id,
            items=[("card", EvidenceItem(
                eid=doc_eid(doc.id), kind="document",
                text=doc.title or ss.event_summary,
                source_url=doc.url, source_tier=doc.source_tier, published_at=ss.published_at,
            ))],
            method="rule",
        )
    ss.proc_status = "published"


_MIN_AWARE = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def _aware(d: dt.datetime | None) -> dt.datetime:
    """Normalize to tz-aware so naive (SQLite) and aware datetimes sort together."""
    if d is None:
        return _MIN_AWARE
    return d if d.tzinfo is not None else d.replace(tzinfo=dt.timezone.utc)


def recompute_ranks(db: Session) -> None:
    """S-10: rank published cards within each pillar (threats first, then recency)."""
    cards = db.scalars(select(SrvSignal)).all()
    by_pillar: dict[str, list[SrvSignal]] = {}
    for c in cards:
        by_pillar.setdefault(c.pillar, []).append(c)

    for pillar_cards in by_pillar.values():
        pillar_cards.sort(
            key=lambda c: (_DIR_WEIGHT.get(c.dir, 0), _aware(c.published_at)),
            reverse=True,
        )
        for i, c in enumerate(pillar_cards, start=1):
            c.rank = i
