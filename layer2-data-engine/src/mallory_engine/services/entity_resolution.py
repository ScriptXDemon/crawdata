"""S-05 Entity resolution — confirm/repair the crawler's competitor link against ref_competitors.

Deterministic: exact id match first, then alias/substring match over the document text. No LLM.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.reference import RefCompetitor


def _alias_index(db: Session) -> list[tuple[str, str]]:
    """Return (lowercased alias, competitor_id), longest aliases first to prefer specific matches."""
    pairs: list[tuple[str, str]] = []
    for comp in db.scalars(select(RefCompetitor)).all():
        names = [comp.name, *(comp.aliases or [])]
        for n in names:
            if n:
                pairs.append((n.lower(), comp.id))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def _valid_ids(db: Session) -> set[str]:
    return set(db.scalars(select(RefCompetitor.id)).all())


def resolve_competitor(db: Session, competitor_id: str | None, text: str | None) -> str | None:
    """Resolve to a ref_competitors id, or None if no confident match."""
    valid = _valid_ids(db)
    if competitor_id and competitor_id in valid:
        return competitor_id
    if text:
        hay = text.lower()
        for alias, cid in _alias_index(db):
            if alias in hay:
                return cid
    return None
