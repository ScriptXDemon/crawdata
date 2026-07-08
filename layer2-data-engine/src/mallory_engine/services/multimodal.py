"""Multimodal analysis — turns the crawler's captured assets into intel (Phase B).

The crawler stores images/PDFs/screenshots on the document but nothing analyzed them.
This stage does two deterministic-first things, both gated so they're a clean no-op when
the vision model is absent or there are no assets:

  * IMAGES → vision model caption + recognised labels; labels are alias-matched against
    known products so a recognised system becomes a citable fact.
  * PDFs → spec rows pulled from the crawler's already-extracted PDF text (fast model),
    appended to the owning tender's ``requirement_fields`` so tender scoring picks them up
    with zero scoring changes.

Idempotent: an asset is analyzed once (a StgAssetAnalysis row exists for it). Runs as an
opt-in pass (``/ops/analyze-assets``) rather than every micro-batch, because the vision
model must swap into VRAM (24GB can't hold it alongside the 14b) — keep the hot extraction
path free of swap stalls.
"""

from __future__ import annotations

import base64
import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.reference import RefCompetitorProduct, RefKsslProduct
from ..models.staging import StgAssetAnalysis, StgDocument, StgTender
from . import asset_client
from .evidence import EvidenceItem, write_evidence

# common image content-types by extension (for the data: URI)
_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
         "gif": "image/gif", "webp": "image/webp"}


def _data_uri(storage_path: str) -> str | None:
    """Fetch asset bytes from L1 and wrap as a base64 data: URI for the vision model."""
    try:
        raw = asset_client.fetch_asset(storage_path)
    except Exception:
        return None
    ext = storage_path.rsplit(".", 1)[-1].lower() if "." in storage_path else "jpg"
    mime = _MIME.get(ext, "image/jpeg")
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _product_index(db: Session) -> dict[str, str]:
    """Lowercased product name/alias → product id, for alias-matching vision labels."""
    idx: dict[str, str] = {}
    for p in db.scalars(select(RefKsslProduct)).all():
        idx[p.name.lower()] = p.id
        for a in (p.aliases or []):
            idx[str(a).lower()] = p.id
    for cp in db.scalars(select(RefCompetitorProduct)).all():
        idx[cp.name.lower()] = cp.id
        for a in (cp.aliases or []):
            idx[str(a).lower()] = cp.id
    return idx


def _match_labels(labels: list[str], prod_idx: dict[str, str]) -> list[str]:
    """Return product ids for any label that alias-matches a known product."""
    hits: list[str] = []
    for label in labels or []:
        low = str(label).lower()
        for name, pid in prod_idx.items():
            if name and name in low:
                hits.append(pid)
                break
    return hits


def _already(db: Session, doc_id: str) -> set[str]:
    """Keys ('{kind}:{index}') already analyzed for this document."""
    rows = db.scalars(
        select(StgAssetAnalysis).where(StgAssetAnalysis.document_id == doc_id)
    ).all()
    return {f"{r.asset_kind}:{r.asset_index}" for r in rows}


def analyze_document_assets(db: Session, llm, doc: StgDocument,
                            prod_idx: dict[str, str]) -> dict[str, int]:
    """Analyze one document's un-analyzed assets. Returns counts per kind."""
    counts = {"images": 0, "pdfs": 0, "specs": 0}
    done = _already(db, doc.id)
    now = dt.datetime.now(dt.timezone.utc)

    # ── images → vision caption + label match ──
    for i, img in enumerate(doc.images or []):
        if f"image:{i}" in done or not isinstance(img, dict):
            continue
        sp = img.get("storage_path")
        if not sp:
            continue
        uri = _data_uri(sp)
        cap = llm.caption_image(image_uri=uri, context=doc.title or "") if uri else {}
        matched = _match_labels(cap.get("labels", []), prod_idx) if cap else []
        db.add(StgAssetAnalysis(
            document_id=doc.id, asset_kind="image", asset_index=i, storage_path=sp,
            method="vision_llm", caption=cap.get("caption"),
            labels=(cap.get("labels") or None), status="ok" if cap else "empty",
            created_at=now,
        ))
        counts["images"] += 1
        # a recognised, captioned image is citable evidence on the document's signal
        if cap.get("caption"):
            write_evidence(
                db, target_kind="document_asset", target_id=f"{doc.id}#img{i}",
                items=[("caption", EvidenceItem(
                    eid=f"img:{doc.id}#{i}", kind="image",
                    text=cap["caption"], source_url=doc.url))],
                method="llm", replace=True)

    # ── PDFs → spec extraction from already-extracted text ──
    tender = None  # resolve lazily; most docs aren't tenders
    for i, att in enumerate(doc.attachments or []):
        if f"pdf:{i}" in done or not isinstance(att, dict):
            continue
        text = att.get("extracted_text")
        if not text:
            continue
        out = llm.extract_specs(pdf_text=text)
        specs = out.get("specs") or []
        db.add(StgAssetAnalysis(
            document_id=doc.id, asset_kind="pdf", asset_index=i,
            storage_path=att.get("storage_path"), method="pdf_text",
            extracted_specs=(specs or None), status="ok" if specs else "empty",
            created_at=now,
        ))
        counts["pdfs"] += 1
        # merge into the owning tender's requirement_fields (tender scoring picks it up)
        if specs:
            if tender is None:
                tender = db.scalars(
                    select(StgTender).where(StgTender.document_id == doc.id).limit(1)
                ).first()
            if tender is not None:
                fields = list(tender.requirement_fields or [])
                have = {(f.get("label"), f.get("value")) for f in fields}
                for s in specs:
                    key = (s.get("label"), s.get("value"))
                    if s.get("label") and key not in have:
                        fields.append({"label": s["label"], "value": s.get("value", "")})
                        counts["specs"] += 1
                tender.requirement_fields = fields
                if tender.proc_status == "published":
                    tender.proc_status = "received"  # re-score with the new specs

    return counts


def analyze_pending_assets(db: Session, llm) -> dict[str, int]:
    """Analyze assets for every document that has un-analyzed images/PDFs.

    Only touches documents that actually carry assets; a no-op when none do or when the
    vision model is disabled (caption_image returns {} → empty rows, no crash).
    """
    totals = {"docs": 0, "images": 0, "pdfs": 0, "specs": 0}
    prod_idx = _product_index(db)
    docs = db.scalars(select(StgDocument)).all()
    for doc in docs:
        if not (doc.images or doc.attachments):
            continue
        done = _already(db, doc.id)
        n_assets = len(doc.images or []) + len(doc.attachments or [])
        if len(done) >= n_assets:  # fully analyzed already
            continue
        counts = analyze_document_assets(db, llm, doc, prod_idx)
        if any(counts.values()):
            totals["docs"] += 1
            for k, v in counts.items():
                totals[k] += v
    db.commit()
    return totals
