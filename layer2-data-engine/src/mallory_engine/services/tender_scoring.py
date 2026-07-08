"""S-12/S-13 Tender normalizer + scoring engine.

Parses a tender's requirements into slots, scores each KSSL product in the matching category by
spec comparison, builds fit % + up/down match lines, and asks the LLM for the go/maybe/pass verdict.
This is the canonical "new tender → auto-scored vs all KSSL products" flow.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.reference import RefKsslProduct, RefProductSpec
from ..models.serving import SrvTender, SrvTenderMatch
from ..models.staging import StgDocument, StgTender
from .llm import LLMProvider
from .spec_extract import parse_requirements, polarity_for, slot_for, unit_for

# Rough display-only FX (replace with ext_fx_rates / S-04 in production).
_FX_TO_USD = {"INR": 1 / 83.0, "EUR": 1.08, "GBP": 1.27, "USD": 1.0}

# Back-compat aliases (slot logic now lives in spec_extract, shared with matchups/multimodal).
_slot_for = slot_for
_parse_requirements = parse_requirements


def _kssl_specs(db: Session, product_id: str) -> dict[str, float]:
    rows = db.scalars(
        select(RefProductSpec).where(
            RefProductSpec.product_side == "kssl", RefProductSpec.product_id == product_id
        )
    ).all()
    specs: dict[str, float] = {}
    for r in rows:
        slot = _slot_for(r.spec_label)
        if slot and r.value_num is not None:
            specs[slot] = float(r.value_num)
    return specs


def _score_product(reqs: dict, specs: dict, product_name: str) -> tuple[int, list[list[str]]]:
    """Return (fit_pct, match_lines) for one KSSL product against parsed requirements."""
    score = 55  # category already matches
    lines: list[list[str]] = []
    for slot, (op, req_val) in reqs.items():
        ksv = specs.get(slot)
        unit = unit_for(slot)
        if ksv is None:
            continue
        ok = (
            (op == ">=" and ksv >= req_val)
            or (op == "<=" and ksv <= req_val)
            or (op == "==" and abs(ksv - req_val) < 1e-6)
            or (polarity_for(slot) == "higher_better" and ksv >= req_val)
        )
        label = slot.split("_")[0]
        if ok:
            score += 14
            lines.append(["up", f"{label} {ksv:g}{unit} meets the {req_val:g}{unit} bar"])
        else:
            score -= 8
            lines.append(["down", f"{label} {ksv:g}{unit} vs required {req_val:g}{unit}"])
    return max(5, min(98, score)), lines


def _fit_level(pct: int) -> str:
    return "high" if pct >= 80 else "medium" if pct >= 55 else "low"


def _value_usd(value_num: float | None, currency: str | None) -> float | None:
    if value_num is None:
        return None
    return round(float(value_num) * _FX_TO_USD.get((currency or "USD").upper(), 1.0))


def process_tender(db: Session, llm: LLMProvider, st: StgTender) -> None:
    category_id = st.category_hint
    reqs = _parse_requirements(st.requirement_fields)

    products = db.scalars(
        select(RefKsslProduct).where(RefKsslProduct.category_id == category_id)
    ).all()

    matches: list[SrvTenderMatch] = []
    best_pct = 0
    for p in products:
        pct, lines = _score_product(reqs, _kssl_specs(db, p.id), p.name)
        best_pct = max(best_pct, pct)
        matches.append(
            SrvTenderMatch(
                tender_id=st.id,
                kssl_product_id=p.id,
                kssl_product_name=p.name,
                fit_level=_fit_level(pct),
                fit_pct=pct,
                match_lines=lines,
            )
        )
    matches.sort(key=lambda m: m.fit_pct, reverse=True)

    n_high = sum(1 for m in matches if m.fit_level == "high")
    summary = (
        f"{len(matches)} KSSL product(s) in-category; {n_high} strong fit."
        if matches
        else "No KSSL product in this category."
    )
    verdict = llm.tender_verdict(title=st.title, best_fit_pct=best_pct, match_summary=summary)

    # Deadline → days remaining + status
    dl_days = (st.deadline_date - dt.date.today()).days if st.deadline_date else None
    status = (
        "closed" if dl_days is not None and dl_days < 0
        else "closing" if dl_days is not None and dl_days <= 7
        else "open"
    )

    doc = db.get(StgDocument, st.document_id)
    db.merge(
        SrvTender(
            id=st.id,
            title=st.title,
            issuer=st.issuer,
            country=st.country,
            category=category_id,
            value_display=st.value_raw,
            value_usd=_value_usd(st.value_num, st.value_currency),
            qty=st.qty_raw,
            deadline_date=st.deadline_date,
            dl_days=dl_days,
            req_note=st.requirement_text,
            requirements=[{"label": f["label"], "value": f["value"]} for f in (st.requirement_fields or [])],
            lean=verdict["lean"],
            lean_text=verdict["lean_text"],
            status=status,
            source_url=doc.url if doc else None,
        )
    )
    db.query(SrvTenderMatch).filter(SrvTenderMatch.tender_id == st.id).delete()
    for m in matches:
        db.add(m)

    st.value_usd = _value_usd(st.value_num, st.value_currency)
    st.category_id = category_id
    st.proc_status = "published"
