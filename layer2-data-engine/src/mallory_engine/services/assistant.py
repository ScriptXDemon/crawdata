"""S-26 Mallory chat + S-25 CEO report — grounded over the serving tables.

Mallory answers ONLY from the scoped serving rows for the panel the user is in. The report composes
a cross-pillar brief from the top serving rows. Both use the LLM provider (OpenRouter/Gemini or stub).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts.serving import MalloryRequest, MalloryResponse, ReportResponse
from ..models.serving import (
    SrvCompetitorSynthesis,
    SrvInnovation,
    SrvPartnership,
    SrvSignal,
    SrvSignalDetail,
    SrvTender,
)
from .llm import LLMProvider

ANCHOR = "KSSL"
SYSTEM = (
    f"You are Mallory, {ANCHOR}'s competitive-intelligence analyst. Answer ONLY from the provided "
    f"context, always framed as what it means for {ANCHOR}. If the answer is not in the context, say "
    f"you don't have that data. Be concise and concrete."
)


def _signal_context(db: Session, signal_id: int) -> tuple[str, list[str]]:
    d = db.get(SrvSignalDetail, signal_id)
    s = db.get(SrvSignal, signal_id)
    if not d and not s:
        return "", []
    parts = [f"SIGNAL: {d.title if d else s.title}"]
    if s:
        parts.append(f"Direction vs {ANCHOR}: {s.dir}. {s.sowhat or ''}")
    if d:
        if d.facts:
            parts.append("Facts: " + "; ".join(f"{k}={v}" for k, v in d.facts))
        if d.why_text:
            parts.append(f"Why it matters: {d.why_text}")
        for lens, read in d.lens_reads or []:
            parts.append(f"[{lens}] {read}")
    return "\n".join(parts), [d.title if d else s.title]


def _tender_context(db: Session, tender_id: int) -> tuple[str, list[str]]:
    from ..models.serving import SrvTenderMatch

    t = db.get(SrvTender, tender_id)
    if not t:
        return "", []
    parts = [
        f"TENDER: {t.title} ({t.issuer}, {t.country}). Value {t.value_display}, deadline in "
        f"{t.dl_days} days. {ANCHOR} lean: {t.lean}. {t.lean_text or ''}"
    ]
    matches = db.scalars(select(SrvTenderMatch).where(SrvTenderMatch.tender_id == tender_id)).all()
    for m in matches:
        parts.append(f"{m.kssl_product_name}: {m.fit_pct}% fit ({m.fit_level}).")
    return "\n".join(parts), [t.title]


def _competitor_context(db: Session, competitor_id: str) -> tuple[str, list[str]]:
    syn = db.get(SrvCompetitorSynthesis, competitor_id)
    parts: list[str] = []
    sources: list[str] = []
    if syn:
        parts.append(f"COMPETITOR {syn.competitor_name}: {syn.thesis}")
        parts.append(f"So what vs {ANCHOR}: {syn.strat_sowhat}")
        for v in syn.vulnerabilities or []:
            parts.append(f"Vulnerability — {v.get('title')}: {v.get('intel')}")
        sources.append(f"{syn.competitor_name} synthesis")
    parts_p = db.scalars(
        select(SrvPartnership).where(SrvPartnership.competitor_id == competitor_id)
    ).all()
    for p in parts_p[:6]:
        parts.append(f"Partnership: {p.partner_name} ({p.rel_type}) — {p.kssl_relevance}.")
    return "\n".join(parts), sources or [competitor_id]


def _overview_context(db: Session) -> tuple[str, list[str]]:
    rows = db.scalars(select(SrvSignal).order_by(SrvSignal.rank).limit(8)).all()
    parts = [f"[{s.pillar}/{s.dir}] {s.title} — {s.sowhat or ''}" for s in rows]
    return "TOP SIGNALS\n" + "\n".join(parts), [s.title for s in rows[:3]]


def answer(db: Session, llm: LLMProvider, req: MalloryRequest) -> MalloryResponse:
    ctx, sources = "", []
    if req.panel_context == "signal" and req.entity_id:
        ctx, sources = _signal_context(db, int(req.entity_id))
    elif req.panel_context == "tender" and req.entity_id:
        ctx, sources = _tender_context(db, int(req.entity_id))
    elif req.panel_context == "competitor" and req.entity_id:
        ctx, sources = _competitor_context(db, req.entity_id)
    else:
        ctx, sources = _overview_context(db)

    text = llm.chat(system=SYSTEM, context=ctx, message=req.message)
    return MalloryResponse(answer=text, scope=req.panel_context, sources=sources)


def ceo_report(db: Session, llm: LLMProvider, focus: str | None) -> ReportResponse:
    threats = db.scalars(
        select(SrvSignal).where(SrvSignal.dir == "threat").order_by(SrvSignal.rank).limit(5)
    ).all()
    go = db.scalars(select(SrvTender).where(SrvTender.lean == "go").limit(5)).all()
    innov = db.scalars(select(SrvInnovation).limit(5)).all()

    ctx = "THREATS:\n" + "\n".join(f"- {s.title}: {s.sowhat or ''}" for s in threats)
    ctx += "\n\nGO TENDERS:\n" + "\n".join(f"- {t.title} ({t.lean})" for t in go)
    ctx += "\n\nINNOVATION:\n" + "\n".join(f"- {i.title} [{i.gap_vs_kssl}]" for i in innov)

    summary = llm.chat(
        system=SYSTEM,
        context=ctx,
        message=focus or f"Write a 3-sentence executive summary for {ANCHOR} leadership of the "
        f"current competitive picture.",
    )

    sections = [
        {"heading": "Executive summary", "body": summary},
        {"heading": "Top competitive threats",
         "body": [{"title": s.title, "note": s.sowhat} for s in threats]},
        {"heading": "Tender priorities (pursue)",
         "body": [{"title": t.title, "note": t.lean_text} for t in go]},
        {"heading": "Technology watch",
         "body": [{"title": i.title, "note": f"{i.gap_vs_kssl} · {i.impact}"} for i in innov]},
    ]
    return ReportResponse(
        title=f"{ANCHOR} — CEO Competitive Brief",
        generated_at=dt.datetime.now(tz=dt.timezone.utc),
        sections=sections,
    )
