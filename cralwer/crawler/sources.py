"""Source identity + trust tier resolution (L2-confirmed design, 2026-06-30).

Every document gets a ``source_id`` + ``source_tier`` plus two provenance flags
(``source_known``, ``source_resolved_by``). Precedence, cheapest/most-authoritative first:

  1. JOB-STAMPED  — the orchestrator stamps source_id/tier/type/region on the job
                    (it owns the master Source Catalog). Used VERBATIM.               -> resolved_by="job"
  2. REGISTRY     — curated known outlets in source_registry.json (Janes=1, MoD=1…).  -> resolved_by="registry"
  3. HEURISTIC    — classify an unknown domain by category (gov/manufacturer/…).      -> resolved_by="heuristic"
  4. FALLBACK     — nothing matched -> aggregator, tier 3, source_known=false.        -> resolved_by="fallback"

Hard rules from L2:
  * NEVER dump unknowns into a tier-1 catch-all (the old COMPANY_IR behavior is dead).
    Fail to LOW trust (tier 3), never tier 1.
  * ``source_id`` is derived from the REGISTRABLE domain (eTLD+1, public-suffix aware),
    so www./m./news. subdomains collapse to ONE id (raksha-anirveda.com -> RAKSHAANIRVEDA).
  * TIER != RELEVANCE. Tier is metadata only; it never drops/downranks a record here.
    A tier-3 blog can carry the most important signal — L2 decides importance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import tldextract

from .seed import Seed

# Offline: use the packaged public-suffix snapshot only (no network fetch).
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())

# Category -> default trust tier (agreed taxonomy).
CATEGORY_TIER = {
    "gov_primary": 1, "manufacturer_ir": 1, "defence_org": 1, "tender_portal": 1,
    "trade_press": 2, "think_tank": 2, "business_press": 2,
    "aggregator": 3, "blog_forum_social": 3, "unknown": 3,
}

# --- curated heuristic sets (small seeds; the orchestrator's catalog is authoritative) ---
MANUFACTURER_DOMAINS = {
    "bharatforge.com", "larsentoubro.com", "lnteds.com", "rheinmetall.com",
    "baesystems.com", "elbitsystems.com", "lockheedmartin.com", "knds.com",
    "tataadvancedsystems.com", "solargroup.com", "nibe.co.in", "bel-india.in",
    "bdl-india.in", "adani-defence.com",
}
DEFENCE_ORG_DOMAINS = {
    "nato.int", "nspa.nato.int", "occar.int", "europa.eu", "eda.europa.eu", "un.org",
}
TENDER_PORTAL_DOMAINS = {"ted.europa.eu", "gem.gov.in"}
TRADE_PRESS_DOMAINS = {
    "navalnews.com", "defenseone.com", "thedefensepost.com", "militarytimes.com",
    "flightglobal.com", "nationaldefensemagazine.org", "defensescoop.com",
    "aviationweek.com", "overtdefense.com", "militaryafrica.com", "defenceweb.co.za",
}
THINK_TANK_DOMAINS = {
    "rand.org", "csis.org", "iiss.org", "idsa.in", "orfonline.org", "brookings.edu",
    "sipri.org", "rusi.org", "atlanticcouncil.org", "hudson.org", "cnas.org",
    "carnegieendowment.org", "claws.in", "vifindia.org",
}
BUSINESS_PRESS_DOMAINS = {
    "reuters.com", "bloomberg.com", "ft.com", "livemint.com", "moneycontrol.com",
    "cnbc.com", "wsj.com", "forbes.com",
}

# ccTLD -> region (best-effort; only for the heuristic path).
_CC_REGION = {
    "in": "India", "uk": "UK", "fr": "France", "de": "Germany", "it": "Italy",
    "se": "Sweden", "il": "Israel", "kr": "South Korea", "cn": "China", "ru": "Russia",
    "us": "USA", "au": "Australia", "ca": "Canada", "za": "South Africa", "eu": "Europe",
}


@dataclass
class SourceInfo:
    source_id: str
    source_tier: int
    source_type: str
    source_region: str | None
    source_known: bool
    source_resolved_by: str          # job | registry | heuristic | fallback


def _parts(url: str) -> tuple[str, str, str, str]:
    """Return (host, domain_label, suffix, registrable_domain)."""
    host = (urlsplit(url).hostname or "").lower()
    e = _EXTRACT(host)
    registrable = f"{e.domain}.{e.suffix}" if e.suffix else e.domain
    return host, e.domain, e.suffix, registrable


def mint_source_id(url: str) -> str:
    """Stable id from the registrable domain's label: raksha-anirveda.com -> RAKSHAANIRVEDA.
    Subdomains (www./m./news.) collapse to the same id."""
    host, domain, _suffix, _reg = _parts(url)
    base = domain or host
    sid = re.sub(r"[^a-z0-9]", "", base.lower()).upper()
    return sid or "UNKNOWNSOURCE"


def _region(suffix: str) -> str | None:
    last = (suffix or "").split(".")[-1]
    return _CC_REGION.get(last)


def _is_gov(host: str, suffix: str) -> bool:
    h = host.lower()
    if h.endswith((".gov", ".mil")) or ".gov." in h or ".mil." in h:
        return True
    if h.endswith((".nic.in", ".gouv.fr")):
        return True
    parts = (suffix or "").split(".")
    return "gov" in parts or "mil" in parts


def _classify(url: str) -> SourceInfo:
    """Heuristic category -> tier for a domain not in the registry. Fails SAFE to
    aggregator/tier-3/source_known=false (never tier 1)."""
    host, _domain, suffix, registrable = _parts(url)
    sid = mint_source_id(url)
    region = _region(suffix)

    def _si(category: str, known: bool, by: str) -> SourceInfo:
        return SourceInfo(sid, CATEGORY_TIER[category], category, region, known, by)

    if _is_gov(host, suffix):
        return _si("gov_primary", True, "heuristic")
    if registrable in MANUFACTURER_DOMAINS:
        return _si("manufacturer_ir", True, "heuristic")
    if registrable in DEFENCE_ORG_DOMAINS:
        return _si("defence_org", True, "heuristic")
    if registrable in TENDER_PORTAL_DOMAINS:
        return _si("tender_portal", True, "heuristic")
    if registrable in TRADE_PRESS_DOMAINS:
        return _si("trade_press", True, "heuristic")
    if registrable in THINK_TANK_DOMAINS:
        return _si("think_tank", True, "heuristic")
    if registrable in BUSINESS_PRESS_DOMAINS:
        return _si("business_press", True, "heuristic")
    # Nothing matched -> fail safe to LOW trust.
    return SourceInfo(sid, 3, "aggregator", region, False, "fallback")


def resolve_source(url: str, seed: Seed, job=None) -> SourceInfo:
    # 1) job-stamped identity — used verbatim (orchestrator is the authority).
    job_sid = getattr(job, "source_id", None) if job else None
    if job_sid:
        stype = getattr(job, "source_type", None) or "unknown"
        stier = getattr(job, "source_tier", None)
        if stier is None:
            stier = CATEGORY_TIER.get(stype, 3)
        return SourceInfo(job_sid, stier, stype,
                          getattr(job, "source_region", None), True, "job")

    # 2) curated registry (real-domain match only; no wildcard catch-all).
    src = seed.source_for_url(url)
    if src is not None:
        return SourceInfo(src.id, src.tier, src.type, src.region, True, "registry")

    # 3) + 4) heuristic classify / fail-safe fallback.
    return _classify(url)
