"""Evidence identity + write helper — the grounding contract.

Every citable thing has a stable evidence-id (eid) built on existing row identity, so a srv_*
row can always be traced back to the documents and rows that produced it. Rule-produced and
LLM-produced fields both write evidence here, so the /explain endpoint is uniform.

eid scheme:
    doc:<id>          stg_documents row
    sig:/tender:/part:/geo:/innov:/event:<id>   corresponding stg_* row
    spec:<id>         ref_product_specs row
    ref:<table>:<id>  any reference fact
    img:<doc>#<n> / att:<doc>#<n> / shot:<doc>   asset within a document
    agg:<name>:<hash> deterministic aggregate (carries member eids)
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..models.serving import SrvEvidence


def doc_eid(document_id: str) -> str:
    return f"doc:{document_id}"


def stg_eid(kind: str, row_id) -> str:
    return f"{kind}:{row_id}"


@dataclass
class EvidenceItem:
    """A citable unit passed to LLM packs and written to srv_evidence."""
    eid: str
    kind: str
    text: str  # <= ~400 chars
    source_url: str | None = None
    source_tier: int | None = None
    published_at: dt.datetime | None = None

    def clipped(self) -> str:
        return (self.text or "")[:400]


def write_evidence(
    db: Session,
    *,
    target_kind: str,
    target_id: str | int,
    items: list[tuple[str, EvidenceItem]],
    method: str = "rule",
    llm_run_id: int | None = None,
    replace: bool = True,
) -> None:
    """Write evidence links for a target. ``items`` is a list of (field, EvidenceItem).

    ``replace`` clears prior evidence for this target so re-processing is idempotent.
    """
    tid = str(target_id)
    if replace:
        db.execute(
            delete(SrvEvidence).where(
                SrvEvidence.target_kind == target_kind, SrvEvidence.target_id == tid
            )
        )
    for field, item in items:
        db.add(SrvEvidence(
            target_kind=target_kind, target_id=tid, field=field, evidence_id=item.eid,
            quote=item.clipped(), source_url=item.source_url, source_tier=item.source_tier,
            published_at=item.published_at, method=method, llm_run_id=llm_run_id,
        ))
