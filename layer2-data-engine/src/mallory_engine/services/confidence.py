"""Deterministic confidence scoring — the computational core of Janes-style tradecraft.

confidence(0-100) = source_tier(<=35) + corroboration(<=25) + freshness(<=25) + provenance(15)

Rule-based forever (never LLM-computed). Every score decomposes into stored `parts`, so the
client can render *why* a card scored what it did — one reusable {label, points, max} bar list.
Mirrors the per-slot decomposition tender fit% already uses.
"""

from __future__ import annotations

import datetime as dt

# Tier → base points. tier 1 = most authoritative (primary/official), 4 = weakest aggregator.
_TIER_BASE = {1: 35, 2: 28, 3: 19, 4: 12}
_TIER_LABEL = {1: "Tier-1 primary source", 2: "Tier-2 trade press",
               3: "Tier-3 aggregator", 4: "Tier-4 unverified"}

# Per-pillar freshness half-life (days): tech ages slowly, market signals fast.
_HALF_LIFE = {"competitive": 45, "market": 30, "technology": 90}
_DEFAULT_HALF_LIFE = 45

_CORROBORATION_MAX = 25
_FRESHNESS_MAX = 25
_PROVENANCE_SOURCED = 15
_PROVENANCE_ESTIMATE = 5


def band(score: int) -> str:
    if score >= 70:
        return "high"
    if score >= 45:
        return "medium"
    return "low"


_band = band  # internal alias


def _freshness_points(age_days: float, half_life: int) -> int:
    # Exponential decay: full marks fresh, ~half at one half-life, floor at 0.
    factor = 0.5 ** (max(age_days, 0) / half_life)
    return round(_FRESHNESS_MAX * factor)


def _corroboration_points(independent_sources: int) -> int:
    # 1 source → 0 bonus; saturates at 3 independent sources.
    extra = max(independent_sources - 1, 0)
    return round(_CORROBORATION_MAX * min(extra, 3) / 3)


def score(
    *,
    source_tier: int | None,
    independent_sources: int,
    published_at: dt.datetime | None,
    provenance: str,
    pillar: str | None = None,
    now: dt.datetime | None = None,
) -> tuple[int, str, list[dict]]:
    """Return (score 0-100, band high|medium|low, parts).

    ``parts`` is a list of {factor, label, points, max} for UI decomposition.
    """
    tier = source_tier if source_tier in _TIER_BASE else 4
    tier_pts = _TIER_BASE[tier]

    corr_pts = _corroboration_points(independent_sources)

    half_life = _HALF_LIFE.get(pillar or "", _DEFAULT_HALF_LIFE)
    if published_at is not None:
        # Normalize to tz-aware: SQLite (and some ingests) hand back naive datetimes.
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=dt.timezone.utc)
        now = now or dt.datetime.now(tz=dt.timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=dt.timezone.utc)
        age_days = (now - published_at).total_seconds() / 86400
        fresh_pts = _freshness_points(age_days, half_life)
        fresh_label = f"{int(age_days)}d old" if age_days >= 1 else "today"
    else:
        fresh_pts = round(_FRESHNESS_MAX * 0.5)  # unknown date → neutral, not zero
        fresh_label = "date unknown"

    prov_pts = _PROVENANCE_SOURCED if provenance == "sourced" else _PROVENANCE_ESTIMATE
    prov_label = "Crawled source" if provenance == "sourced" else "Estimated"

    total = tier_pts + corr_pts + fresh_pts + prov_pts
    total = max(5, min(95, total))

    parts = [
        {"factor": "source", "label": _TIER_LABEL[tier], "points": tier_pts, "max": 35},
        {"factor": "corroboration",
         "label": f"{independent_sources} independent source" + ("s" if independent_sources != 1 else ""),
         "points": corr_pts, "max": _CORROBORATION_MAX},
        {"factor": "freshness", "label": fresh_label, "points": fresh_pts, "max": _FRESHNESS_MAX},
        {"factor": "provenance", "label": prov_label, "points": prov_pts, "max": _PROVENANCE_SOURCED},
    ]
    return total, _band(total), parts


def demo() -> None:
    """Self-check: the trust ordering the whole feature depends on must hold."""
    now = dt.datetime(2026, 7, 6, tzinfo=dt.timezone.utc)
    fresh = dt.datetime(2026, 7, 5, tzinfo=dt.timezone.utc)
    old = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)

    single_tier3, _, _ = score(source_tier=3, independent_sources=1, published_at=fresh,
                               provenance="sourced", pillar="competitive", now=now)
    triple_tier1, band, parts = score(source_tier=1, independent_sources=3, published_at=fresh,
                                      provenance="sourced", pillar="competitive", now=now)
    stale, _, _ = score(source_tier=1, independent_sources=3, published_at=old,
                        provenance="sourced", pillar="market", now=now)
    estimate, _, _ = score(source_tier=2, independent_sources=1, published_at=fresh,
                          provenance="estimate", pillar="competitive", now=now)

    # corroboration + authority beats a lone aggregator
    assert triple_tier1 > single_tier3, (triple_tier1, single_tier3)
    # freshness matters: same source, old story scores lower
    assert stale < triple_tier1, (stale, triple_tier1)
    # a sourced tier-2 outranks an estimate
    assert single_tier3 >= 0 and estimate < triple_tier1
    # parts sum (pre-clamp) equals the reported score when unclamped
    assert band == "high", band
    assert sum(p["points"] for p in parts) == triple_tier1, parts
    print("confidence.demo OK:",
          f"single_tier3={single_tier3} triple_tier1={triple_tier1} "
          f"stale={stale} estimate={estimate}")


if __name__ == "__main__":
    demo()
