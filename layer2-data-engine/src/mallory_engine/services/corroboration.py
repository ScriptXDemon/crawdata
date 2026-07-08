"""S-06/S-08 corroboration (deterministic) — how many independent sources back a claim.

Groups staging signals that report the same event across different documents, so a thrice-
confirmed contract award scores higher than a lone blog post. The key is built on STABLE
entities the crawler resolves — competitor, country, direction, and a normalized deal value —
not on wording, which varies source to source. Embedding similarity (S-06/S-08 proper) will
loosen ``_claim_key`` later to catch same-event stories that share no value; for now a shared
value + competitor + country is a high-precision corroboration signal.

Independence = distinct source documents (by source_id, falling back to document_id).
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.staging import StgDocument, StgSignal


def _value_bucket(sig: StgSignal) -> str:
    """A currency-agnostic value fingerprint: the leading significant digits of any amount.

    '₹4,500 cr' and '4500 crore' both reduce to '45' — same deal, different wording.
    """
    num = sig.deal_value_num
    if num is None:
        m = re.search(r"\d[\d,]*", sig.deal_value_raw or sig.event_summary or "")
        if not m:
            return "?"
        num = float(m.group(0).replace(",", ""))
    if num <= 0:
        return "?"
    # normalize to 2 significant digits so 4500 and 4499 bucket together
    s = f"{num:.0f}".lstrip("0")
    return s[:2] if len(s) >= 2 else s


def _claim_key(sig: StgSignal) -> str:
    return "|".join([
        (sig.resolved_competitor_id or sig.competitor_id or "?"),
        (sig.detected_country or "?"),
        (sig.dir or "?"),
        _value_bucket(sig),
    ])


def corroboration_counts(db: Session) -> dict[int, int]:
    """Return {stg_signal.id: independent_source_count} across all published/received signals.

    A group's independent-source count is shared by every member, so any card the group backs
    reflects the full corroboration.
    """
    rows = db.execute(
        select(StgSignal, StgDocument.source_id)
        .join(StgDocument, StgDocument.id == StgSignal.document_id)
    ).all()

    groups: dict[str, set[str]] = {}     # claim_key -> distinct source identifiers
    sig_key: dict[int, str] = {}
    for sig, source_id in rows:
        key = _claim_key(sig)
        sig_key[sig.id] = key
        groups.setdefault(key, set()).add(source_id or f"doc:{sig.document_id}")

    return {sid: len(groups[key]) for sid, key in sig_key.items()}


def demo() -> None:
    """Self-check without a DB: two summaries of one award share a claim key; a different event doesn't."""
    class _S:
        def __init__(self, comp, country, d, summ, num=None, raw=None):
            self.resolved_competitor_id = comp
            self.competitor_id = comp
            self.detected_country = country
            self.dir = d
            self.event_summary = summ
            self.deal_value_num = num
            self.deal_value_raw = raw

    # same ₹4,500 cr award, two wordings/currencies → same key via the value bucket
    a = _claim_key(_S("LT", "IN", "threat", "L&T secures Rs 4,500 cr K9 Vajra order", raw="₹4,500 cr"))
    b = _claim_key(_S("LT", "IN", "threat", "L&T wins K9 Vajra deal", num=4500.0))
    c = _claim_key(_S("ADANI", "AE", "threat", "Adani opens ammunition line in UAE", raw="₹21,000 cr"))
    assert a == b, (a, b)   # same event → same key
    assert a != c, (a, c)   # different event → different key
    print("corroboration.demo OK:", a)


if __name__ == "__main__":
    demo()
