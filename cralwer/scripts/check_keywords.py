"""Standalone keyword-relevance probe for a single URL.

Fetches one URL and reports which keywords appear on the page, using the
EXACT SAME bounded-match function the Stage-2 gate uses
(``gate._keyword_hits`` — case-insensitive, word-boundary matching, so "BEL"
does not match inside "rebel"). This is a fast pre-flight check, separate
from the full crawler pipeline: no BFS crawl, no entity resolution, no
dedup writes, no asset capture, no ingest POST.

It also runs the REAL gate (``gate.evaluate``) against the same fetched page
and prints both results side by side, so you can directly compare this
probe's keyword-only verdict against the actual pipeline's full gate verdict
(keyword AND entity match) on the same page.

Usage:
  CRAWLER_ALLOW_NETWORK=1 python scripts/check_keywords.py <url> <keyword1> [keyword2 ...]

Example:
  CRAWLER_ALLOW_NETWORK=1 python scripts/check_keywords.py \\
      https://www.lntpes.com/ artillery defence submarine
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root on path

os.environ.setdefault("CRAWLER_ALLOW_NETWORK", "1")     # this is a LIVE fetch by default
os.environ.setdefault("CRAWLER_PREFER_FIXTURES", "0")   # not a fixture — go straight to the web

from crawler import parse, textextract
from crawler.fetcher import Fetcher
from crawler.gate import _keyword_hits, evaluate
from crawler.models import Job
from crawler.resolver import build_matcher, resolve
from crawler.seed import load_seed


def check_keywords(url: str, keywords: list[str], render_js: bool = False) -> dict:
    """Fetch *url* and report which *keywords* appear on the page (title +
    main text), using the same matching function as the gate. Returns a
    dict describing the fetch outcome and the keyword hits — never raises."""
    seed = load_seed()
    fetcher = Fetcher(user_agent=seed.capture_defaults["user_agent"],
                      timeout_s=seed.capture_defaults.get("timeout_seconds", 30),
                      delay_s=0, render_js=render_js)
    res = fetcher.fetch(url)

    if res.error or res.status is None or res.status >= 400 or not res.text_html:
        return {
            "url": url, "final_url": res.final_url, "status": res.status,
            "error": res.error or f"http_{res.status}",
            "matched": False, "matched_keywords": [], "keywords_checked": keywords,
            "title": None, "text_chars": 0,
        }

    title = parse.title_of(res.text_html) or ""
    text = textextract.main_text(res.text_html)
    hits = _keyword_hits(f"{title}\n{text}", keywords)  # same fn the gate uses

    return {
        "url": url, "final_url": res.final_url, "status": res.status, "error": None,
        "matched": bool(hits), "matched_keywords": hits, "keywords_checked": keywords,
        "title": title, "text_chars": len(text), "_text": text, "_title": title,
    }


def discover_keywords(url: str, candidate_pool: list[str],
                      render_js: bool = False) -> dict:
    """Probe-adaptive keyword selection: fetch *url* and return the subset of
    *candidate_pool* that actually appears on the page — those become a job's
    keywords. Same fetch + bounded matcher (gate._keyword_hits) as
    check_keywords, so selected keywords can never disagree with the gate.
    Returns {url, status, title, pool_size, selected_keywords, error}."""
    probe = check_keywords(url, candidate_pool, render_js=render_js)
    return {
        "url": probe["url"], "status": probe["status"], "error": probe["error"],
        "title": probe.get("title"), "pool_size": len(candidate_pool),
        "selected_keywords": probe["matched_keywords"],
    }


def compare_with_gate(url: str, keywords: list[str], render_js: bool = False) -> None:
    """Run this probe AND the real pipeline gate against the same page, then
    print both verdicts side by side."""
    probe = check_keywords(url, keywords, render_js=render_js)

    print(f"URL:      {url}")
    print(f"Status:   {probe['status']}  error={probe['error']}")
    if probe["error"]:
        print("\nCould not fetch page — nothing to compare.")
        return

    print(f"Title:    {probe['title']!r}")
    print(f"Text:     {probe['text_chars']} chars\n")

    print("=== PROBE (keyword-only, this script) ===")
    print(f"  matched:          {probe['matched']}")
    print(f"  matched_keywords: {probe['matched_keywords']}")

    # Run the REAL gate on the same fetched text for a direct comparison.
    seed = load_seed()
    matcher = build_matcher(seed)
    job = Job(job_id="check_keywords_probe", job_type="news", seed_urls=[url],
             keywords=keywords, target_entity=None)
    detected = resolve(probe["_text"], probe["_title"], seed, matcher)
    gate_result = evaluate(job, probe["_title"], probe["_text"], detected, None)

    print("\n=== GATE (real pipeline: keyword-relevance filter, gate.evaluate) ===")
    print(f"  keep:             {gate_result.keep}")
    print(f"  reason:           {gate_result.reason}")
    print(f"  matched_keywords: {gate_result.matched_keywords}")
    print(f"  entities:         {[(d.surface, d.resolved_id, d.type) for d in detected][:8]}")

    print("\n=== COMPARISON ===")
    same = set(probe["matched_keywords"]) == set(gate_result.matched_keywords)
    print(f"  keyword hits agree: {same}")
    if not same:
        print(f"  !! DIVERGENCE: probe={probe['matched_keywords']} "
              f"gate={gate_result.matched_keywords}")
    if probe["matched"] and not gate_result.keep:
        print("  Note: probe says keywords matched but the gate dropped the "
              f"page (reason={gate_result.reason}) — the only keyword-relevant "
              "drop left is stale-beyond-freshness. Entity resolution is "
              "info-only now and never drops a page.")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    url, *keywords = argv
    if not keywords:
        print("error: at least one keyword is required")
        return 2
    compare_with_gate(url, keywords)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
