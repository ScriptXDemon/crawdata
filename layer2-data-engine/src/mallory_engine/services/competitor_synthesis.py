"""S-23 Competitor synthesis — the analyst brain: strategic read per competitor, from evidence.

Stages: GATHER (published srv rows → cited evidence items) → SYNTHESIZE (deep model, one
structured call over the evidence — never raw documents) → VERIFY (schema + validators inside
the task layer; here: output non-empty and evidence floor met) → PUBLISH (provenance='sourced',
confidence from the cited rows, per-field srv_evidence links).

Fail-safe: a bad or thin generation NEVER overwrites an existing row — the seed 'estimate'
stays until real evidence produces something better.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models.reference import RefCompetitor, RefKsslProduct
from ..models.serving import (
    SrvCompetitorSynthesis,
    SrvGeoEntry,
    SrvPartnership,
    SrvSignal,
)
from . import confidence as conf
from .evidence import EvidenceItem, write_evidence

_MAX_ITEMS = 60


def _gather(db: Session, comp: RefCompetitor) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    signals = db.scalars(
        select(SrvSignal).where(SrvSignal.company == comp.name)
        .order_by(SrvSignal.confidence.desc().nullslast(), SrvSignal.published_at.desc().nullslast())
    ).all()
    for s in signals:
        items.append(EvidenceItem(
            eid=f"sig:{s.id}", kind="signal",
            text=f"({s.dir}, conf {s.confidence or '?'}) {s.title}",
            source_url=s.source_url, published_at=s.published_at,
        ))
    for p in db.scalars(select(SrvPartnership).where(SrvPartnership.competitor_id == comp.id)).all():
        items.append(EvidenceItem(
            eid=f"part:{p.id}", kind="partnership",
            text=f"partnership: {p.partner_name} ({p.rel_type or '?'}) — {p.meaning or ''}",
            source_url=p.source_url,
        ))
    for g in db.scalars(select(SrvGeoEntry).where(SrvGeoEntry.competitor_id == comp.id)).all():
        items.append(EvidenceItem(
            eid=f"geo:{g.id}", kind="geo",
            text=f"geo: {g.country} — {g.product_name or '?'} ({g.stage or '?'})",
            source_url=g.source_url,
        ))
    return items[:_MAX_ITEMS]


def _render(items: list[EvidenceItem]) -> str:
    return "\n".join(f"[{i.eid}] {i.clipped()}" for i in items)


def _exemplar(exclude_id: str) -> str:
    """A seed entry sets the analytical quality bar — but NEVER the competitor being
    synthesized, or the model leaks exemplar claims into the output as if they were evidence."""
    path = Path(get_settings().seed_dir) / "competitor_synthesis.json"
    try:
        for s in json.loads(path.read_text(encoding="utf-8")).get("synthesis", []):
            if s.get("competitor_id") != exclude_id:
                ex = {k: s[k] for k in ("thesis", "strat_sowhat", "vulnerabilities") if k in s}
                # The exemplar must teach the cites MECHANIC too — seed vulns lack the field,
                # and a model that imitates the shape verbatim would omit cites and get dropped.
                for v in ex.get("vulnerabilities", []):
                    v["cites"] = ["<ids of the evidence items that support this, e.g. sig:12>"]
                return json.dumps(ex, ensure_ascii=False)
    except Exception:
        pass
    return ('{"thesis": "<one sharp sentence: what this competitor fundamentally is vs KSSL>", '
            '"vulnerabilities": [{"title": "<weakness>", "intel": "<the evidence-backed read>", '
            '"cites": ["sig:12", "part:3"]}]}')


def _anchor_frame(db: Session) -> str:
    products = db.scalars(select(RefKsslProduct)).all()
    return "KSSL products: " + ", ".join(f"{p.name} ({p.category_id})" for p in products[:12])


def _synthesis_confidence(db: Session, cites: list[str]) -> int | None:
    """Confidence of a synthesis = mean confidence of the signal rows it cites. Grounded, simple."""
    sig_ids = [int(c.split(":", 1)[1]) for c in cites if c.startswith("sig:")]
    if not sig_ids:
        return None
    rows = db.scalars(select(SrvSignal.confidence).where(SrvSignal.id.in_(sig_ids))).all()
    vals = [r for r in rows if r is not None]
    return round(sum(vals) / len(vals)) if vals else None


def synthesize_competitor(db: Session, llm, competitor_id: str, *,
                          min_evidence: int = 3) -> dict:
    """Run the full S-23 chain for one competitor. Returns a status dict for ops."""
    comp = db.get(RefCompetitor, competitor_id)
    if comp is None or comp.is_anchor:
        return {"competitor": competitor_id, "status": "skipped", "reason": "unknown or anchor"}

    items = _gather(db, comp)
    existing = db.get(SrvCompetitorSynthesis, competitor_id)

    if len(items) < min_evidence:
        # Not enough real evidence — never degrade an existing (seed) row.
        return {"competitor": competitor_id, "status": "kept",
                "reason": f"only {len(items)} evidence items (< {min_evidence})"}

    gen = getattr(llm, "synthesize_competitor", None)
    out = gen(competitor=comp.name, anchor_frame=_anchor_frame(db),
              pack_text=_render(items), pack_ids={i.eid for i in items},
              exemplar=_exemplar(exclude_id=competitor_id)) if gen else {}

    if not out.get("thesis") or not out.get("vulnerabilities"):
        # Generation failed schema/validators (or no capable provider) → fail-safe: keep row.
        return {"competitor": competitor_id, "status": "kept",
                "reason": "generation failed or provider lacks synthesis"}

    # Tradecraft rule: an uncited vulnerability does not ship. This is what stops
    # exemplar/parametric-knowledge claims from laundering into 'sourced' intelligence.
    from .llm.tasks import norm_cite
    pack_eids = {i.eid for i in items}
    for v in out["vulnerabilities"]:
        v["cites"] = [norm_cite(c) for c in (v.get("cites") or [])]
    out["vulnerabilities"] = [
        v for v in out["vulnerabilities"]
        if any(c in pack_eids for c in v["cites"])
    ]
    if not out["vulnerabilities"]:
        return {"competitor": competitor_id, "status": "kept",
                "reason": "no vulnerability survived citation check"}

    all_cites = list(out.get("cites") or [])
    for v in out["vulnerabilities"]:
        all_cites.extend(v.get("cites") or [])
    score = _synthesis_confidence(db, all_cites)

    db.merge(SrvCompetitorSynthesis(
        competitor_id=competitor_id, competitor_name=comp.name,
        thesis=out["thesis"], strat_sowhat=out.get("strat_sowhat"),
        vulnerabilities=[{"title": v["title"], "intel": v["intel"]}
                         for v in out["vulnerabilities"]],
        predictions=out.get("predictions"), moves=out.get("moves"),
        gaps=out.get("gaps"), provenance="sourced",
        confidence=score, confidence_band=conf.band(score) if score is not None else None,
        updated_at=dt.datetime.now(dt.timezone.utc),
    ))

    by_eid = {i.eid: i for i in items}
    ev_links: list[tuple[str, EvidenceItem]] = []
    for c in out.get("cites") or []:
        if c in by_eid:
            ev_links.append(("thesis", by_eid[c]))
    for n, v in enumerate(out["vulnerabilities"]):
        for c in v.get("cites") or []:
            if c in by_eid:
                ev_links.append((f"vulnerability:{n}", by_eid[c]))
    write_evidence(db, target_kind="synthesis", target_id=competitor_id,
                   items=ev_links, method="llm")

    return {"competitor": competitor_id, "status": "sourced",
            "evidence": len(items), "cited": len(ev_links), "confidence": score,
            "was": existing.provenance if existing else None}


def synthesize_all(db: Session, llm, *, min_evidence: int = 3) -> list[dict]:
    comps = db.scalars(select(RefCompetitor).where(RefCompetitor.is_anchor.is_(False))).all()
    return [synthesize_competitor(db, llm, c.id, min_evidence=min_evidence) for c in comps]
