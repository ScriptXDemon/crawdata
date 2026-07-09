"""S-05 Entity resolution — confirm/repair the crawler's competitor link against ref_competitors.

Deterministic: exact id match first, then WORD-BOUNDARY alias match over the document text. No LLM.

Word boundaries matter: a naive substring match lets a short alias like 'PEL' (Premier
Explosives) match inside 'proPELlant', 'BEL' inside 'laBEL', 'LT' inside 'defauLT'. We guard
alphanumeric aliases with \\b...\\b (same rule the crawler's resolver uses), so only whole-token
matches count.
"""

from __future__ import annotations

import re
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.reference import RefCompetitor


def _alias_index(db: Session) -> list[tuple[str, str, re.Pattern]]:
    """Return (alias, competitor_id, compiled word-boundary pattern), longest aliases first."""
    pairs: list[tuple[str, str, re.Pattern]] = []
    for comp in db.scalars(select(RefCompetitor)).all():
        names = [comp.name, *(comp.aliases or [])]
        for n in names:
            if n and n.strip():
                pairs.append((n.lower(), comp.id, _boundary_pattern(n.lower())))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def _boundary_pattern(alias: str) -> re.Pattern:
    """Word-boundary match for alphanumeric aliases; substring for ones with spaces/punctuation
    (e.g. 'l&t defence' — \\b is unreliable around '&'/spaces, and multiword is already specific)."""
    if re.fullmatch(r"[a-z0-9]+", alias):
        return re.compile(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])")
    return re.compile(re.escape(alias))


def _valid_ids(db: Session) -> set[str]:
    return set(db.scalars(select(RefCompetitor.id)).all())


def resolve_competitor(db: Session, competitor_id: str | None, text: str | None) -> str | None:
    """Resolve to a ref_competitors id, or None if no confident match."""
    valid = _valid_ids(db)
    if competitor_id and competitor_id in valid:
        return competitor_id
    if text:
        hay = text.lower()
        for _alias, cid, pat in _alias_index(db):
            if pat.search(hay):
                return cid
    return None
