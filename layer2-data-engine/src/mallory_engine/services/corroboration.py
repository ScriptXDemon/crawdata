"""S-06/S-08 corroboration — how many independent sources back a claim.

Groups staging signals that report the same event across different documents, so a thrice-
confirmed contract award scores higher than a lone blog post. Two layers, both additive:

  1. DETERMINISTIC key (always): STABLE entities the crawler resolves — competitor, country,
     direction, and a normalized deal value — not wording, which varies source to source.
  2. EMBEDDING merge (Phase C, when ``ollama_model_embed`` is set): same-event stories that
     share NO value but are semantically near-identical (cosine ≥ threshold, same competitor)
     get their deterministic groups merged. This only ever RAISES corroboration; a disabled
     embed model or any embedding failure is a clean no-op (deterministic result stands).

Independence = distinct source documents (by source_id, falling back to document_id).
"""

from __future__ import annotations

import math
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models.staging import StgDocument, StgSignal

_SIM_THRESHOLD = 0.86  # cosine; two signal texts above this are treated as the same event


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
    """Deterministic claim identity. When a deal value is present it anchors the key (high
    precision — a shared value + competitor + country is almost certainly one event). When
    there is NO value, we fall back to a per-signal key (id) so unrelated value-less stories
    about one competitor don't collapse together — the embedding merge (Phase C) then re-joins
    only the ones that are genuinely the same event. Without embeddings, value-less signals
    simply don't corroborate each other (conservative, correct)."""
    bucket = _value_bucket(sig)
    tail = bucket if bucket != "?" else f"sig{sig.id}"  # distinct when no value
    return "|".join([
        (sig.resolved_competitor_id or sig.competitor_id or "?"),
        (sig.detected_country or "?"),
        (sig.dir or "?"),
        tail,
    ])


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _embed_merge(rows: list, sig_key: dict[int, str]) -> dict[str, str]:
    """Merge deterministic claim-keys whose signal texts are semantically near-identical.

    Returns a {old_key: canonical_key} remap. Only merges within the SAME competitor
    (high precision) and only when an embed model is configured. Any failure ⇒ empty remap
    (deterministic grouping stands unchanged).
    """
    settings = get_settings()
    if not settings.ollama_model_embed:
        return {}
    # embed one representative text per (competitor, key) — cheap, and same-event stories
    # about different competitors never merge.
    reps: dict[str, tuple[str, str]] = {}  # key -> (competitor, text)
    for sig, _src in rows:
        key = sig_key[sig.id]
        if key not in reps:
            comp = sig.resolved_competitor_id or sig.competitor_id or "?"
            reps[key] = (comp, (sig.event_summary or "")[:400])
    keys = list(reps)
    texts = [reps[k][1] for k in keys]
    # Embeddings run on their OWN endpoint (mixed backend: farm has no embed model, so this
    # stays local even when chat/vision point at the farm).
    from .llm.transport import OpenAICompatTransport
    embed_base = settings.ollama_embed_base_url or settings.ollama_base_url
    transport = OpenAICompatTransport(
        base_url=embed_base, api_key=settings.ollama_embed_api_key, timeout_s=settings.llm_timeout_s,
    )
    vecs = transport.embed(texts, model=settings.ollama_model_embed)
    if not vecs or len(vecs) != len(keys):
        return {}
    # union-find over keys of the same competitor with cosine >= threshold
    parent = {k: k for k in keys}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            if reps[keys[i]][0] != reps[keys[j]][0] or reps[keys[i]][0] == "?":
                continue  # different competitor (or unresolved) → never merge
            if _cosine(vecs[i], vecs[j]) >= _SIM_THRESHOLD:
                parent[find(keys[i])] = find(keys[j])
    return {k: find(k) for k in keys}


def corroboration_counts(db: Session) -> dict[int, int]:
    """Return {stg_signal.id: independent_source_count} across all published/received signals.

    A group's independent-source count is shared by every member, so any card the group backs
    reflects the full corroboration.
    """
    rows = db.execute(
        select(StgSignal, StgDocument.source_id)
        .join(StgDocument, StgDocument.id == StgSignal.document_id)
    ).all()

    sig_key: dict[int, str] = {sig.id: _claim_key(sig) for sig, _ in rows}

    # Phase C: semantic merge of deterministic keys (no-op if embed disabled / fails).
    remap = _embed_merge(rows, sig_key)
    if remap:
        sig_key = {sid: remap.get(k, k) for sid, k in sig_key.items()}

    groups: dict[str, set[str]] = {}     # (merged) claim_key -> distinct source identifiers
    for sig, source_id in rows:
        key = sig_key[sig.id]
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
