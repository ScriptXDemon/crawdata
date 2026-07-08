"""S-24 Field patterns — what keeps happening across the whole competitive field.

Deterministic aggregates first (they ARE patterns): shared-partner pile-ups, contested
countries, licensing concentration — each a citable `agg:` evidence unit carrying its member
rows. The LLM (when capable) writes the narrative over aggregates + syntheses; the fallback
publishes the aggregates directly. Only rows with provenance='sourced' feed the aggregates,
so patterns never launder seed estimates into field truths.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.serving import (
    SrvCompetitorSynthesis,
    SrvFieldPattern,
    SrvGeoEntry,
    SrvPartnership,
)
from .evidence import EvidenceItem, write_evidence


@dataclass
class Aggregate:
    eid: str          # 'agg:shared_partner:elbit'
    title: str
    summary: str
    member_eids: list[str] = field(default_factory=list)

    def item(self) -> EvidenceItem:
        return EvidenceItem(eid=self.eid, kind="agg",
                            text=f"{self.title}: {self.summary} [{', '.join(self.member_eids)}]")


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")


def compute_aggregates(db: Session) -> list[Aggregate]:
    aggs: list[Aggregate] = []

    # Shared-partner pile-ups: one org underpinning >= 2 rivals (the chokepoint pattern).
    partner_map: dict[str, list[SrvPartnership]] = {}
    for p in db.scalars(
        select(SrvPartnership).where(SrvPartnership.provenance == "sourced")
    ).all():
        partner_map.setdefault(p.partner_name, []).append(p)
    for partner, rows in partner_map.items():
        comps = sorted({r.competitor_name or r.competitor_id or "?" for r in rows})
        if len(comps) >= 2:
            aggs.append(Aggregate(
                eid=f"agg:shared_partner:{_slug(partner)}",
                title=f"{partner} underpins {len(comps)} rivals",
                summary=f"{partner} has ties to {', '.join(comps)} — a shared dependency/chokepoint.",
                member_eids=[f"part:{r.id}" for r in rows],
            ))

    # Contested countries: >= 2 competitors active in one market.
    country_map: dict[str, list[SrvGeoEntry]] = {}
    for g in db.scalars(
        select(SrvGeoEntry).where(SrvGeoEntry.provenance == "sourced")
    ).all():
        if g.country:
            country_map.setdefault(g.country, []).append(g)
    for country, rows in country_map.items():
        comps = sorted({r.competitor_name or r.competitor_id or "?" for r in rows})
        if len(comps) >= 2:
            aggs.append(Aggregate(
                eid=f"agg:contested_country:{_slug(country)}",
                title=f"{country} is contested by {len(comps)} competitors",
                summary=f"{', '.join(comps)} are all active in {country}.",
                member_eids=[f"geo:{r.id}" for r in rows],
            ))

    # Licensing concentration: how much of the field runs on licensed foreign IP.
    lic = [p for rows in partner_map.values() for p in rows
           if (p.rel_type or "").lower() in ("license", "licensing", "licence")]
    if len(lic) >= 2:
        comps = sorted({p.competitor_name or p.competitor_id or "?" for p in lic})
        aggs.append(Aggregate(
            eid="agg:licensing_concentration",
            title=f"{len(lic)} licensing deals across {len(comps)} competitors",
            summary=f"Licensed foreign IP underpins {', '.join(comps)} — indigenous-IP contrast is live.",
            member_eids=[f"part:{p.id}" for p in lic],
        ))

    return aggs


def refresh_field_patterns(db: Session, llm=None) -> dict:
    """Recompute field patterns. Keeps seed rows when no sourced aggregates exist."""
    aggs = compute_aggregates(db)
    if not aggs:
        return {"status": "kept", "reason": "no sourced aggregates yet"}

    patterns: list[dict] = []
    method = "rule"

    gen = getattr(llm, "field_patterns", None)
    if gen is not None:
        synths = db.scalars(select(SrvCompetitorSynthesis)).all()
        synth_text = "\n".join(
            f"[syn:{s.competitor_id}] {s.thesis or ''}" for s in synths if s.thesis)
        out = gen(
            aggregates_text="\n".join(f"[{a.eid}] {a.title}: {a.summary}" for a in aggs),
            synth_text=synth_text,
            pack_ids={a.eid for a in aggs} | {f"syn:{s.competitor_id}" for s in synths},
        )
        if out.get("patterns"):
            patterns = out["patterns"]
            method = "llm"

    if not patterns:  # deterministic fallback: the aggregates ARE the patterns
        patterns = [{"title": a.title, "summary": a.summary, "exceptions": "",
                     "bottom_line": "", "cites": [a.eid]} for a in aggs]

    db.query(SrvFieldPattern).delete()
    by_eid = {a.eid: a for a in aggs}
    for i, p in enumerate(patterns):
        row = SrvFieldPattern(
            title=p["title"], summary=p.get("summary"), exceptions=p.get("exceptions") or None,
            bottom_line=p.get("bottom_line") or None, ord=i, provenance="sourced",
        )
        db.add(row)
        db.flush()
        links = [("pattern", by_eid[c].item()) for c in (p.get("cites") or []) if c in by_eid]
        if links:
            write_evidence(db, target_kind="field_pattern", target_id=row.id,
                           items=links, method=method)
    return {"status": "sourced", "patterns": len(patterns), "aggregates": len(aggs),
            "method": method}
