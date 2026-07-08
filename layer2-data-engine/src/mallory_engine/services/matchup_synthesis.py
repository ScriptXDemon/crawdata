"""S-22 Matchup engine — recompute positioning head-to-heads from reference data.

Deterministic core (LLM never touches the numbers): per-spec leader by polarity, weighted
edge_score with highlight ×2, per-spec `edge_parts` decomposition. The LLM writes only the
verdict prose, grounded in the spec rows; a template verdict ships when the door is closed.
Serving rows are fully replaced on each recompute — srv_matchups is never hand-written.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.reference import RefMatchup
from ..models.serving import SrvMatchup, SrvMatchupSpec
from .evidence import EvidenceItem, write_evidence


def _leader(spec: dict) -> str:
    """Who leads one paired spec row — numeric compare by polarity, else 'tie'."""
    kn, cn, better = spec.get("kssl_num"), spec.get("comp_num"), spec.get("better")
    if kn is None or cn is None or kn == cn:
        return "tie"
    if better == "low":
        return "kssl" if kn < cn else "comp"
    return "kssl" if kn > cn else "comp"


def _compute(specs: list[dict]) -> tuple[int, str, list[dict], list[tuple]]:
    """Return (edge_score, dir, edge_parts, spec_rows) from paired spec rows."""
    kssl_pts = comp_pts = 0.0
    parts: list[dict] = []
    rows: list[tuple] = []
    for s in specs or []:
        lead = _leader(s)
        weight = 2 if s.get("highlight") else 1
        if lead == "kssl":
            kssl_pts += weight
        elif lead == "comp":
            comp_pts += weight
        parts.append({
            "spec": s.get("label", ""), "leader": lead, "weight": weight,
            "kssl": s.get("kssl"), "comp": s.get("comp"),
        })
        rows.append((s.get("label", ""), s.get("comp"), s.get("kssl"), lead))
    edge = max(5, min(95, round(50 + 12 * (kssl_pts - comp_pts))))
    direction = "fav" if edge >= 60 else "threat" if edge < 40 else "watch"
    return edge, direction, parts, rows


def _template_verdict(mu: RefMatchup, edge: int) -> str:
    band = ("KSSL holds the edge" if edge >= 60
            else "Near-parity on measured specs" if edge >= 40
            else f"{mu.comp_name} leads on measured specs")
    kssl_top = (mu.adv_kssl or ["indigenous IP"])[0]
    comp_top = (mu.adv_comp or ["incumbency"])[0]
    return f"{band}. KSSL's strongest card: {kssl_top}. {mu.comp_name}'s: {comp_top}."


def recompute_all(db: Session, llm=None) -> int:
    """Rebuild srv_matchups (+specs +evidence) from ref_matchups. Returns row count."""
    refs = db.scalars(select(RefMatchup)).all()
    db.query(SrvMatchupSpec).delete()
    db.query(SrvMatchup).delete()

    for mu in refs:
        edge, direction, parts, spec_rows = _compute(mu.specs or [])

        verdict, method = _template_verdict(mu, edge), "rule"
        gen = getattr(llm, "matchup_verdict", None)
        if gen is not None:
            spec_text = "\n".join(
                f"[{mu.id}:{i}] {lbl}: KSSL {kv} vs {cv} ({lead})"
                for i, (lbl, cv, kv, lead) in enumerate(spec_rows)
            )
            out = gen(kssl_name=mu.kssl_name, comp_name=mu.comp_name, edge_score=edge,
                      spec_text=spec_text, pack_ids={f"{mu.id}:{i}" for i in range(len(spec_rows))})
            if out.get("verdict"):
                verdict, method = out["verdict"], "llm"

        row = SrvMatchup(
            category=mu.category_id, dir=direction, country=mu.country,
            comp_name=mu.comp_name, comp_by=mu.comp_by, kssl_name=mu.kssl_name,
            edge_score=edge, adv_comp=mu.adv_comp, adv_kssl=mu.adv_kssl,
            verdict=verdict, edge_parts=parts, provenance="estimate",  # specs are admin-curated
            verdict_method=method,
        )
        db.add(row)
        db.flush()
        for label, cval, kval, lead in spec_rows:
            db.add(SrvMatchupSpec(matchup_id=row.id, spec_label=label, comp_value=cval,
                                  kssl_value=kval, leader=lead))
        # Evidence chain: the matchup card rests on its curated ref row.
        write_evidence(
            db, target_kind="matchup", target_id=row.id,
            items=[("card", EvidenceItem(
                eid=f"ref:ref_matchups:{mu.id}", kind="ref",
                text=f"{mu.kssl_name} vs {mu.comp_name}: {len(spec_rows)} paired specs",
            ))],
            method=method,
        )
    return len(refs)
