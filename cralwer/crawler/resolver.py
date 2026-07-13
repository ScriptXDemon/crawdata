"""Entity resolution (§6) — mechanical, required, never a judgment of importance.

Match surface forms in a document's title + main_text against the seed alias
indexes (entities, products) and keyword sets (tech domains, countries). Produce
``entities_detected[]`` with ``resolved_id`` + ``confidence``.

A defence company *not* in the seed but matching a company-name pattern is
reported as ``resolved_id=null, type="unknown_company"`` — that is how Layer 2
discovers new competitors to add (we flag, never drop).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import EntityDetected
from .seed import Seed

# Countries we recognize = the seed's tender target list + partner countries +
# a few defence-relevant extras. Mechanical surface match only.
_EXTRA_COUNTRIES = [
    "China", "Russia", "Spain", "Sweden", "Switzerland", "Greece", "Qatar",
    "Bangladesh", "Nepal", "Myanmar", "Kazakhstan", "Brazil", "Pakistan",
    "Europe", "United States", "United Kingdom",
]
# Phrase-aliases that resolve to a country even when the bare name doesn't appear
# (e.g. "Indian Army" — "India" alone fails the word boundary inside "Indian").
_COUNTRY_ALIASES: dict[str, list[str]] = {
    "India": ["Indian Army", "Indian Navy", "Indian Air Force", "Indian armed forces",
              "Indian government", "Government of India"],
    "USA": ["U.S. Army", "US Army", "United States government", "U.S. government"],
}


def _confidence(alias: str) -> float:
    """Specificity heuristic: longer / multi-word / punctuated aliases are less
    ambiguous than bare short acronyms."""
    a = alias.strip()
    if len(a) >= 6 or " " in a or "&" in a or "-" in a:
        return 0.97
    if len(a) >= 4:
        return 0.9
    return 0.82          # short acronyms (GD, BEL, MIL) — still resolved, lower conf


_CLUE_RE = re.compile(
    r"\bwon\b|\bawarded\b|\bsigned\b|\bdeployed\b|\bordered\b|\bselected\b|"
    r"\bcontract\b|\bdelivered\b", re.IGNORECASE)
_CLUE_BOOST = 0.02
_CLUE_WINDOW = 60


def _clue_boost(haystack: str, start: int, end: int) -> float:
    """+0.02 if a contract/deployment clue word appears within _CLUE_WINDOW
    chars of the match. Additive only — never lowers confidence, never the
    sole basis for a match."""
    lo, hi = max(0, start - _CLUE_WINDOW), min(len(haystack), end + _CLUE_WINDOW)
    return _CLUE_BOOST if _CLUE_RE.search(haystack[lo:hi]) else 0.0


@dataclass
class _Matcher:
    kp: "object"                              # flashtext.KeywordProcessor
    # One alias string can legitimately mean more than one thing (e.g. "Nagastra"
    # is both a product alias AND a UAV tech_domain example keyword) — flashtext
    # only carries one clean_name per exact keyword, so we index every (typ, rid,
    # surface) registered under that string here and fan a single trie hit back
    # out into all of them.
    registrations: dict[str, list[tuple[str, str, str]]]


def build_matcher(seed: Seed) -> _Matcher:
    from flashtext import KeywordProcessor
    kp = KeywordProcessor(case_sensitive=False)
    registrations: dict[str, list[tuple[str, str, str]]] = {}

    def register(surface: str, rid: str, typ: str) -> None:
        key = surface.lower()
        if key not in registrations:
            kp.add_keyword(surface, key)
        registrations.setdefault(key, []).append((typ, rid, surface))

    for e in seed.entities.values():
        etype = {"competitor": "competitor", "anchor": "anchor",
                 "partner": "partner"}.get(e.kind, "competitor")    
        for surface in (e.name, *e.aliases):
            register(surface, e.id, etype)
    for p in seed.products.values():
        for surface in (p.name, *p.aliases):
            register(surface, p.id, "product")

    countries = list(dict.fromkeys([*seed.tender_countries, *_EXTRA_COUNTRIES]))
    for c in countries:
        register(c, c, "country")
    for cid, aliases in _COUNTRY_ALIASES.items():
        for surface in aliases:
            register(surface, cid, "country")

    for t in seed.tech_domains.values():
        # The domain name + each keyword maps a mention to the domain id.
        for surface in (t.name, *t.keywords):
            register(surface, t.id, "tech_domain")

    return _Matcher(kp=kp, registrations=registrations)


# Company-name shapes used to *discover* defence companies not yet in the seed.
# Inter-word gaps are spaces/tabs only ([ \t]+, never \n) so the pattern can't
# span the title→body newline and glue unrelated tokens into a fake company.
_UNKNOWN_COMPANY_RE = re.compile(
    r"\b([A-Z][A-Za-z&.\-]+(?:[ \t]+[A-Z][A-Za-z&.\-]+){0,3})[ \t]+"
    r"(Defence|Defense|Systems|Industries|Aerospace|Technologies|Dynamics|"
    r"Armoury|Armouring|Ordnance|Arms|Weapons)\b"
)


def resolve(text: str, title: str, seed: Seed, matcher: _Matcher,
            discover_unknown: bool = True) -> list[EntityDetected]:
    """Resolve all seed surfaces in ``title + text``. One EntityDetected per
    distinct resolved_id (highest-confidence surface wins). Adds unknown_company
    candidates for company-shaped names that resolve to nothing."""
    haystack = f"{title}\n{text}"
    best: dict[tuple[str, str | None], EntityDetected] = {}
    first_pos: dict[tuple[str, str | None], int] = {}

    for alias_key, start, end in matcher.kp.extract_keywords(haystack, span_info=True):
        for typ, rid, surface in matcher.registrations.get(alias_key, ()):
            conf = min(1.0, _confidence(surface) + _clue_boost(haystack, start, end))
            key = (typ, rid)
            prev = best.get(key)
            if prev is None or conf > prev.confidence:
                best[key] = EntityDetected(
                    surface=surface, resolved_id=rid, type=typ, confidence=conf)
            first_pos[key] = min(first_pos.get(key, start), start)

    # Order by first appearance so the page's primary subject is element [0]
    # (record extractors pick countries[0] / products[0] / competitors[0]).
    detected = sorted(best.values(),
                      key=lambda d: first_pos.get((d.type, d.resolved_id), 1 << 30))

    if discover_unknown:
        known_surfaces = {d.surface.lower() for d in detected}
        seen_unknown: set[str] = set()
        for m in _UNKNOWN_COMPANY_RE.finditer(haystack):
            name = m.group(0).strip()
            low = name.lower()
            # Skip if this surface is actually a known seed alias (or contains one).
            if low in seed.entity_alias_index or low in known_surfaces:
                continue
            if any(k in low or low in k for k in known_surfaces):
                continue
            if low in seen_unknown:
                continue
            seen_unknown.add(low)
            detected.append(EntityDetected(
                surface=name, resolved_id=None, type="unknown_company",
                confidence=0.4))

    return detected


# --- convenience selectors for the record extractors ---------------------
def competitors(detected: list[EntityDetected]) -> list[str]:
    return [d.resolved_id for d in detected
            if d.type in ("competitor", "anchor") and d.resolved_id]


def products(detected: list[EntityDetected]) -> list[str]:
    return [d.resolved_id for d in detected if d.type == "product" and d.resolved_id]


def countries(detected: list[EntityDetected]) -> list[str]:
    return [d.resolved_id for d in detected if d.type == "country" and d.resolved_id]


def tech_domains(detected: list[EntityDetected]) -> list[str]:
    return [d.resolved_id for d in detected if d.type == "tech_domain" and d.resolved_id]
