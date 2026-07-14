"""Stage 2 — FILTER (the mechanical keyword-relevance gate, §3).

Keep a page if its title+main_text matches >=1 keyword from the global corpus
(``keywords.load_corpus`` — a FlashText trie over the user's CSV). That's it: the
keyword match is what says "this page is relevant to the crawl". This is
mechanical, not strategic — "does this page mention something we're watching?",
never "is this a threat?" (that is Layer 2).

Freshness is also enforced here: content published older than ``freshness_days``
is dropped (when a published date is known; unknown dates are kept, never
fabricated). An empty/absent corpus fails OPEN (keep-all) so a misconfigured
keyword file can't silently zero the output.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import keywords
from .models import Job


@dataclass
class GateResult:
    keep: bool
    reason: str
    matched_keywords: list[str]


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
             published_iso: str | None, kp) -> GateResult:
    if not passes_freshness(published_iso, job.freshness_days):
        return GateResult(False, "stale_beyond_freshness_days", [])

    # Empty corpus -> fail open (keep-all) so a bad CRAWLER_KEYWORDS_FILE path
    # doesn't silently drop every page. len() on a KeywordProcessor is the trie size.
    if kp is None or len(kp) == 0:
        return GateResult(True, "no_corpus_keep_all", [])

    hits = keywords.find(kp, title, main_text)
    if not hits:
        return GateResult(False, "no_keyword_match", hits)
    return GateResult(True, "keyword_match", hits)
