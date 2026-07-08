"""Coverage-complete job generation from the Source Catalog × the seed.

Fixes every gap in the crawler's first-cut jobgen: uses ALL news sources (not 6), ALL tracked
competitors (incl. P3), folds product names into the query, sweeps ALL target countries for
tenders, and stamps source_id/source_tier/source_type on every job. The matrix is a deterministic
function of (catalog × seed), so the coverage ledger can prove nothing is missed.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from .models import Source
from .seed import Seed

NEWS_CATEGORIES = {"trade_press", "business_press", "aggregator", "think_tank", "gov_primary", "defence_org"}

_MAXP = {"news": 30, "tender": 60, "spec": 10, "profile": 25}
_DEPTH = {"news": 2, "tender": 2, "spec": 1, "profile": 2}
_CAPTURE = {
    "news": ["html", "text", "images", "screenshot"],
    "tender": ["html", "text", "pdf", "screenshot"],
    "spec": ["html", "text", "images", "pdf", "screenshot"],
    "profile": ["html", "text"],
}
_EXPECT = {
    "news": ["competitive_signal", "company_event"],
    "tender": ["tender"],
    "spec": ["competitive_signal"],
    "profile": ["partnership", "geo_footprint"],
}


def _stamp(job: dict, s: Source) -> dict:
    job["source_id"] = s.source_id
    job["source_tier"] = s.tier
    job["source_type"] = s.category
    if s.region:
        job["source_region"] = s.region
    return job


def _base(job_type: str, jid: str, seed_urls: list[str], keywords: list[str], entity: str | None) -> dict:
    return {
        "job_id": jid,
        "job_type": job_type,
        "seed_urls": seed_urls,
        "keywords": keywords,
        "target_entity": entity,
        "max_pages": _MAXP[job_type],
        "max_depth": _DEPTH[job_type],
        "same_domain_only": True,
        "render_js": False,
        "freshness_days": 120,
        "capture": _CAPTURE[job_type],
        "expected_record_types": _EXPECT[job_type],
        "forward_to_ingest": True,
    }


def _news_keywords(seed: Seed, eid: str) -> list[str]:
    e = seed.entities[eid]
    kws = list(dict.fromkeys([e.name, *e.aliases]))[:4]
    kws += seed.products_by_owner.get(eid, [])[:4]  # fold product names in
    return kws[:8]


def generate(sources: list[Source], seed: Seed) -> list[dict]:
    jobs: list[dict] = []
    n = 0

    news_sources = [s for s in sources if s.category in NEWS_CATEGORIES and s.search_template]
    for s in news_sources:
        for eid, e in seed.entities.items():  # ALL competitors, incl. P3 — coverage-complete
            n += 1
            q = quote_plus(f"{(e.aliases[0] if e.aliases else e.name)} defence")
            jobs.append(_stamp(
                _base("news", f"job_news_{eid}_{s.source_id}_{n}",
                      [s.search_template.format(q=q)], _news_keywords(seed, eid), eid), s))

    tender_sources = [s for s in sources if s.category == "tender_portal"]
    for s in tender_sources:
        for country in (seed.countries or [None]):  # sweep ALL target countries
            n += 1
            kws = list(seed.tender_keywords[:12]) + ([country] if country else [])
            url = s.search_template.format(q=quote_plus(country or "tender")) if s.search_template else (s.seed_urls or [""])[0]
            jobs.append(_stamp(_base("tender", f"job_tender_{s.source_id}_{n}", [url], kws, None), s))

    # Direct-target sources (e.g. the offline end-to-end test) — one job per explicit URL.
    for s in sources:
        if not s.seed_urls or s.category == "tender_portal":
            continue
        jt = "spec" if s.category == "manufacturer_ir" else "news"
        for url in s.seed_urls:
            n += 1
            jobs.append(_stamp(_base(jt, f"job_direct_{s.source_id}_{n}", [url], [], None), s))

    return jobs
