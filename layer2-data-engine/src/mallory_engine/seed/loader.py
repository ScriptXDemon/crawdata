"""Load the bundled seed JSON into the ref_* tables. Idempotent (upsert by primary key)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models.reference import (
    RefCategory,
    RefCompetitor,
    RefCompetitorProduct,
    RefCountry,
    RefKsslProduct,
    RefMatchup,
    RefProductSpec,
    RefTechDomain,
)
from ..models.serving import (
    SrvCompetitorSynthesis,
    SrvFieldPattern,
    SrvPatent,
)
from ..services import matchup_synthesis

_CATEGORY_NAMES = {
    "artillery": "Artillery",
    "armoured": "Armoured & protected mobility",
    "small_arms": "Small arms",
    "ammunition": "Ammunition & propellants",
    "missiles_ad": "Missiles & air defence",
    "naval": "Naval / marine",
    "uav": "UAV / ISR / loitering",
    "materials": "Materials & forgings",
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _read(seed_dir: Path, name: str) -> dict:
    path = seed_dir / name
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def load_all(db: Session, seed_dir: str | Path | None = None) -> dict[str, int]:
    seed_dir = Path(seed_dir or get_settings().seed_dir)
    counts: dict[str, int] = {}

    # ── Categories ──
    products = _read(seed_dir, "watchlist_products.json")
    for slug in products.get("categories", []):
        db.merge(RefCategory(id=slug, name=_CATEGORY_NAMES.get(slug, slug.title())))
    counts["categories"] = len(products.get("categories", []))

    # ── Competitors (anchor + rivals) ──
    entities = _read(seed_dir, "watchlist_entities.json")
    anchor = entities.get("anchor")
    n_comp = 0
    if anchor:
        db.merge(RefCompetitor(
            id=anchor["id"], name=anchor["name"], aliases=anchor.get("aliases"),
            hq_country=anchor.get("hq"), threat_level="fav", is_anchor=True, priority="P0",
        ))
        n_comp += 1
    for c in entities.get("competitors", []):
        db.merge(RefCompetitor(
            id=c["id"], name=c["name"], aliases=c.get("aliases"), hq_country=c.get("hq"),
            threat_level=c.get("dir"), is_anchor=False, priority=c.get("priority"),
        ))
        n_comp += 1
    counts["competitors"] = n_comp

    # ── KSSL products ──
    for p in products.get("kssl_products", []):
        db.merge(RefKsslProduct(
            id=p["id"], name=p["name"], category_id=p.get("category"), aliases=p.get("aliases"),
        ))
    counts["kssl_products"] = len(products.get("kssl_products", []))

    # ── Competitor products (keyed by competitor id) ──
    n_cp = 0
    for comp_id, items in products.get("competitor_products", {}).items():
        for item in items:
            pid = f"{comp_id}_{_slug(item['name'])[:24]}"
            db.merge(RefCompetitorProduct(
                id=pid, competitor_id=comp_id, name=item["name"],
                category_id=item.get("category"), aliases=item.get("aliases"),
            ))
            n_cp += 1
    counts["competitor_products"] = n_cp

    # ── Tech domains ──
    tech = _read(seed_dir, "watchlist_tech_domains.json")
    for d in tech.get("domains", []):
        db.merge(RefTechDomain(id=d["id"], name=d["name"], keywords=d.get("keywords")))
    counts["tech_domains"] = len(tech.get("domains", []))

    # ── Countries (tender targets) ──
    tenders = _read(seed_dir, "watchlist_tenders.json")
    for country in tenders.get("target_countries", []):
        db.merge(RefCountry(id=_slug(country), name=country))
    counts["countries"] = len(tenders.get("target_countries", []))

    # ── KSSL product specs (illustrative; replace with verified data via Admin API) ──
    specs = _read(seed_dir, "kssl_product_specs.json")
    n_specs = 0
    for s in specs.get("specs", []):
        db.merge(RefProductSpec(
            id=n_specs + 1, product_id=s["product_id"], product_side="kssl",
            spec_label=s["spec_label"], value_num=s.get("value_num"), value_text=s.get("value_text"),
            unit=s.get("unit"), polarity=s.get("polarity"), is_highlight=s.get("is_highlight", False),
        ))
        n_specs += 1
    counts["kssl_specs"] = n_specs

    # ── Matchups (positioning) → ref_matchups; serving rows recomputed by the S-22 engine ──
    matchups = _read(seed_dir, "matchups.json")
    n_mu = 0
    for mu in matchups.get("matchups", []):
        mu_id = f"{mu.get('kssl_id', _slug(mu['kssl_name']))}__{_slug(mu['comp_name'])}"
        db.merge(RefMatchup(
            id=mu_id, kssl_product_id=mu.get("kssl_id"), kssl_name=mu["kssl_name"],
            comp_name=mu["comp_name"], comp_by=mu.get("comp_by"), country=mu.get("country"),
            category_id=mu.get("category"), specs=mu.get("specs"),
            adv_kssl=mu.get("adv_kssl"), adv_comp=mu.get("adv_comp"),
        ))
        n_mu += 1
    counts["matchups"] = n_mu
    db.flush()
    matchup_synthesis.recompute_all(db)  # template verdicts; LLM verdicts via /ops trigger

    # ── Competitor synthesis + field patterns (seed = estimate FALLBACK only:
    #    never overwrite a row the S-23 engine has published as 'sourced') ──
    syn = _read(seed_dir, "competitor_synthesis.json")
    for s in syn.get("synthesis", []):
        existing = db.get(SrvCompetitorSynthesis, s["competitor_id"])
        if existing is not None and existing.provenance == "sourced":
            continue
        comp = db.get(RefCompetitor, s["competitor_id"])
        db.merge(SrvCompetitorSynthesis(
            competitor_id=s["competitor_id"], competitor_name=comp.name if comp else s["competitor_id"],
            thesis=s.get("thesis"), strat_sowhat=s.get("strat_sowhat"),
            vulnerabilities=s.get("vulnerabilities"), predictions=s.get("predictions"),
            moves=s.get("moves"), provenance="estimate",
        ))
    counts["synthesis"] = len(syn.get("synthesis", []))

    has_sourced_patterns = (
        db.query(SrvFieldPattern).filter(SrvFieldPattern.provenance == "sourced").first()
        is not None
    )
    if not has_sourced_patterns:
        db.query(SrvFieldPattern).delete()
        for i, fp in enumerate(syn.get("field_patterns", [])):
            db.add(SrvFieldPattern(title=fp["title"], summary=fp.get("summary"),
                                   exceptions=fp.get("exceptions"), ord=i))
    counts["field_patterns"] = len(syn.get("field_patterns", []))

    # ── Patents (sample until API connected) ──
    pat = _read(seed_dir, "patents.json")
    for p in pat.get("patents", []):
        db.merge(SrvPatent(
            id=p["id"], competitor_id=p.get("competitor_id"), tech_domain_id=p.get("tech_domain_id"),
            jurisdiction=p.get("jurisdiction"), title=p["title"], status=p.get("status"),
            filed_date=p.get("filed_date"), assignee=p.get("assignee"), abstract=p.get("abstract"),
            kssl_relevance=p.get("kssl_relevance"), provenance="estimate",
        ))
    counts["patents"] = len(pat.get("patents", []))

    db.commit()
    return counts
