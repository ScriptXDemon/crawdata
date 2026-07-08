"""Patent connector — real patents into srv_patents, replacing the seed fiction.

Two sources, tried in order:
  1. USPTO Open Data Portal (api.uspto.gov) — the REAL free API. It replaced PatentsView
     (retired 2025); PatentsView's search.patentsview.org host no longer resolves. Needs a
     free key (data.uspto.gov → My ODP → API keys) sent as X-API-KEY. Used when
     settings.uspto_api_key is set. Thousands of req/day, granted+published US patents.
  2. Google Patents public XHR (keyless) — fallback when no USPTO key. Real EP/US/WO
     bibliographic data but aggressively rate-limited (503s after a handful of calls), so
     only good for a slow trickle. We only READ public data, same as the web UI serves.

Per watched competitor we search its assignee names, take the most recent filings, and map
each to an SrvPatent row with provenance='sourced'. Tech domain is classified against
ref_tech_domains keywords (the crawler's vocabulary), so a patent lands under
artillery / uav / ammunition just like a signal does. Mapping is source-agnostic: both
fetchers return the same {publication_number,title,assignee,filing_date,snippet} shape.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models.reference import RefCompetitor, RefTechDomain
from ..models.serving import SrvPatent

_XHR = "https://patents.google.com/xhr/query?url="
_UA = "Mozilla/5.0 (compatible; mallory-intel/1.0)"

# Competitor id -> assignee search terms (org names as they appear on filings). Only the
# ones with real patent portfolios; the rest fall back to the competitor name.
_ASSIGNEE = {
    "RHEIN": ["Rheinmetall"], "KNDS": ["Nexter", "Krauss-Maffei Wegmann", "KNDS"],
    "HANWHA": ["Hanwha"], "BAE": ["BAE Systems"], "ELBIT": ["Elbit Systems"],
    "LT": ["Larsen & Toubro"], "SOLAR": ["Solar Industries"], "BDL": ["Bharat Dynamics"],
    "GENATOMICS": ["General Atomics"], "OSHKOSH": ["Oshkosh"],
    "AEROVIRONMENT": ["Aerovironment"], "ANDURIL": ["Anduril"],
    "LM": ["Lockheed Martin"], "GD": ["General Dynamics"], "LEO": ["Leonardo"],
    "RAFAEL": ["Rafael"], "NORINCO": ["Norinco"],
}


def _jurisdiction(pub_no: str) -> str | None:
    """US20250300420A1 -> US, EP..., WO..., CN..., KR...."""
    pub_no = (pub_no or "").strip().upper()
    for cc in ("US", "EP", "WO", "CN", "KR", "IN", "DE", "FR", "GB", "IL", "JP"):
        if pub_no.startswith(cc):
            return cc
    return None


def _status(pub_no: str) -> str:
    # Utility grants end in B/C; applications end in A1/A9. Rough but honest.
    pub_no = (pub_no or "").upper()
    return "granted" if ("B" in pub_no[-3:] or pub_no.endswith(("B1", "B2", "C1"))) else "pending"


def _fetch_google(query: str, size: int = 8, timeout: int = 25, retries: int = 3) -> list[dict]:
    """Keyless fallback: Google Patents XHR. Rate-limited; back off on 503."""
    inner = f"q=({query})&type=PATENT&num={size}&sort=new"
    url = _XHR + urllib.parse.quote(inner)
    # Google Patents' XHR checks Referer/Accept; without them it 503s aggressively.
    headers = {
        "User-Agent": _UA, "Accept": "application/json",
        "Referer": "https://patents.google.com/", "X-Requested-With": "XMLHttpRequest",
    }
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            clusters = data.get("results", {}).get("cluster", []) or []
            rows = clusters[0].get("result", []) if clusters else []
            # normalise to the common shape
            return [row.get("patent", {}) for row in rows if row.get("patent")]
        except urllib.error.HTTPError as e:  # 503 = throttled; back off and retry
            last = e
            if e.code in (429, 503) and attempt < retries - 1:
                time.sleep(4 * (attempt + 1))  # 4s, 8s
                continue
            raise
    raise last  # type: ignore[misc]


def _fetch_uspto(query_terms: list[str], api_key: str, base_url: str,
                 size: int = 8, timeout: int = 30) -> list[dict]:
    """USPTO Open Data Portal patent search by assignee. Returns the common shape.

    POST /api/v1/patent/applications/search with X-API-KEY. The body is a query DSL;
    we match the assignee organisation name and sort by filing date desc.
    """
    url = f"{base_url}/api/v1/patent/applications/search"
    # OR across the assignee spellings on the applicant/assignee name field
    q = " OR ".join(f'"{t}"' for t in query_terms)
    body = json.dumps({
        "q": f"applicant.name:({q})",
        "sort": [{"field": "filingDate", "order": "desc"}],
        "pagination": {"offset": 0, "limit": size},
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "X-API-KEY": api_key, "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": _UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    out = []
    for rec in data.get("patentBag") or data.get("results") or data.get("docs") or []:
        # ODP field names vary by endpoint; pull defensively into the common shape.
        out.append({
            "publication_number": rec.get("patentNumber") or rec.get("applicationNumberText")
                                  or rec.get("publicationNumber"),
            "title": rec.get("inventionTitle") or rec.get("title"),
            "assignee": _first_assignee(rec) or (query_terms[0] if query_terms else None),
            "filing_date": rec.get("filingDate"),
            "priority_date": rec.get("effectiveFilingDate"),
            "snippet": rec.get("abstractText") or rec.get("abstract") or "",
        })
    return [o for o in out if o.get("publication_number")]


def _first_assignee(rec: dict):
    for key in ("assigneeBag", "applicantBag", "assignees", "applicants"):
        bag = rec.get(key)
        if isinstance(bag, list) and bag:
            a = bag[0]
            return a.get("name") or a.get("organizationName") if isinstance(a, dict) else a
    return None


def _fetch(query_terms: list[str], size: int, settings) -> list[dict]:
    """Dispatch: USPTO when a key is set, else Google Patents fallback."""
    if settings.serpapi_key:
        return _fetch_serpapi(query_terms, settings.serpapi_key, size=size)
    if settings.uspto_api_key:
        return _fetch_uspto(query_terms, settings.uspto_api_key, settings.uspto_base_url, size=size)
    google_query = " OR ".join(f'"{t}"' for t in query_terms)
    return _fetch_google(google_query, size=size)


def _fetch_serpapi(query_terms: list[str], api_key: str, size: int = 8,
                   timeout: int = 30) -> list[dict]:
    """SerpApi Google Patents (engine=google_patents). Reliable, no throttle. Common shape."""
    # assignee filter: comma-separated; wrap names containing commas in parens
    assignees = ",".join(f"({t})" if "," in t else t for t in query_terms)
    params = urllib.parse.urlencode({
        "engine": "google_patents", "assignee": assignees, "sort": "new",
        "num": size, "api_key": api_key,
    })
    url = f"https://serpapi.com/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    out = []
    for rec in data.get("organic_results") or []:
        out.append({
            "publication_number": rec.get("publication_number") or rec.get("patent_id"),
            "title": rec.get("title"),
            "assignee": rec.get("assignee") or (query_terms[0] if query_terms else None),
            "filing_date": rec.get("filing_date"),
            "priority_date": rec.get("priority_date"),
            "snippet": rec.get("snippet") or "",
        })
    return [o for o in out if o.get("publication_number")]


def _tech_domain(db: Session, text: str) -> str | None:
    low = (text or "").lower()
    for d in db.scalars(select(RefTechDomain)).all():
        if any((k or "").lower() in low for k in (d.keywords or [])):
            return d.id
    return None


def sync_patents(db: Session, per_competitor: int = 6, delay_s: float = 2.0) -> dict[str, int]:
    """Fetch real recent patents for competitors with known assignee portfolios.

    Only queries competitors in _ASSIGNEE (the 24 small Indian PSUs have no filings to
    find — querying them just wastes requests and trips the rate limit). Estimate rows
    are only removed once at least one real patent is fetched, so a total failure leaves
    the seed sample in place rather than an empty table.

    Returns {competitors, fetched, upserted, errors}. Idempotent: upsert by patent id.
    """
    settings = get_settings()
    source = ("SerpApi Google Patents" if settings.serpapi_key
              else "USPTO ODP" if settings.uspto_api_key
              else "Google Patents (keyless)")
    print(f"patent source: {source}")
    counts = {"competitors": 0, "fetched": 0, "upserted": 0, "errors": 0}
    # only competitors we have real assignee names for
    ids = {c.id for c in db.scalars(select(RefCompetitor)).all()}
    targets = [(cid, terms) for cid, terms in _ASSIGNEE.items() if cid in ids]
    dropped_estimates = False

    for c_id, terms in targets:
        counts["competitors"] += 1
        try:
            patents = _fetch(terms, per_competitor, settings)
        except Exception as e:  # network/endpoint change — skip this competitor, keep going
            counts["errors"] += 1
            print(f"  {c_id}: fetch failed ({type(e).__name__}: {e})")
            continue
        if patents and not dropped_estimates:
            # first real data in — now safe to clear the seed sample
            db.query(SrvPatent).filter(SrvPatent.provenance == "estimate").delete()
            db.flush()
            dropped_estimates = True
        counts["fetched"] += len(patents)
        for p in patents:
            pub = p.get("publication_number")
            if not pub:
                continue
            title = (p.get("title") or "").strip()
            db.merge(SrvPatent(
                id=pub,
                competitor_id=c_id,
                tech_domain_id=_tech_domain(db, f"{title} {p.get('snippet','')}"),
                jurisdiction=_jurisdiction(pub),
                title=title or "(untitled)",
                status=_status(pub),
                filed_date=p.get("filing_date") or p.get("priority_date"),
                assignee=(p.get("assignee") or terms[0]),
                abstract=(p.get("snippet") or "").strip() or None,
                kssl_relevance="ADJACENT",
                provenance="sourced",
            ))
            counts["upserted"] += 1
        print(f"  {c_id}: {len(patents)} patents ({', '.join(terms)[:40]})")
        time.sleep(delay_s)  # be polite to the endpoint
    db.commit()
    return counts
