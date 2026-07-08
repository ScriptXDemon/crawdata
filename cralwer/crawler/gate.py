"""Stage 2 — FILTER (the mechanical keyword-relevance gate, §3) + info generator.

Keep a page if it matches ≥1 job keyword. That's it — the keyword match is what
says "this page is relevant to the crawl". Entity resolution is NOT a drop
condition: it runs on every kept page and its result (watched competitor,
country, tech-domain, etc.) is packed onto the document as informational tags
for Layer 2. Whether an entity resolved only labels the keep reason
(``keyword_and_entity_match`` vs ``keyword_match_only``), never the keep/drop
decision. This is mechanical, not strategic — "does this page mention what we're
crawling for?", never "is this a threat?" (that is Layer 2).

Freshness is also enforced here: content published older than ``freshness_days``
is dropped (when a published date is known; unknown dates are kept, never
fabricated).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import EntityDetected, Job


@dataclass
class GateResult:
    keep: bool
    reason: str
    matched_keywords: list[str]


def _keyword_hits(text: str, keywords: list[str]) -> list[str]:
    hits = []
    hay = text.lower()
    for kw in keywords:
        k = kw.lower().strip()
        if not k:
            continue
        # Bound alphanumeric keywords; substring for ones with punctuation/spaces.
        if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", hay):
            hits.append(kw)
    return hits


def _resolves_to_entity(detected: list[EntityDetected]) -> bool:
    # §9 rule 2: a page resolves if it hits ≥1 seed entity / product / tech-domain.
    # (Country alone is too broad to count as resolution — it is descriptive.)
    return any(
        d.resolved_id and d.type in (
            "competitor", "anchor", "partner", "product", "tech_domain")
        for d in detected
    )


def passes_freshness(published_iso: str | None, freshness_days: int | None) -> bool:
    if not freshness_days or not published_iso:
        return True   # no limit, or unknown date -> keep (never fabricate a drop)
    try:
        pub = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)
        return pub >= cutoff
    except Exception:
        return True


def evaluate(job: Job, title: str, main_text: str,
             detected: list[EntityDetected], published_iso: str | None) -> GateResult:
    haystack = f"{title}\n{main_text}"
    hits = _keyword_hits(haystack, job.keywords)

    if not passes_freshness(published_iso, job.freshness_days):
        return GateResult(False, "stale_beyond_freshness_days", hits)

    if not hits:
        return GateResult(False, "no_keyword_match", hits)

    # Keyword match keeps the page. Entity resolution only labels the reason
    # (info for L2), it does not gate the keep decision.
    if _resolves_to_entity(detected):
        return GateResult(True, "keyword_and_entity_match", hits)

    return GateResult(True, "keyword_match_only", hits)
