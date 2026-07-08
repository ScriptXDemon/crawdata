"""One-off LIVE crawl of a single real URL, to prove the pipeline on real data.

Usage: CRAWLER_ALLOW_NETWORK=1 python scripts/live_crawl.py <url>
"""
import os
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root on path

os.environ["CRAWLER_ALLOW_NETWORK"] = "1"     # this is a LIVE fetch
os.environ["CRAWLER_PREFER_FIXTURES"] = "0"   # not a fixture — go straight to the web

from crawler.dedup import CrawlHistory
from crawler.fetcher import Fetcher
from crawler.ingest_client import CollectingIngestClient
from crawler.models import Job
from crawler.pipeline import run_job
from crawler.resolver import build_matcher
from crawler.seed import load_seed

URL = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"

job = Job(
    job_id="live_k9_test_01",
    job_type="news",
    seed_urls=[URL],
    keywords=["K9 Vajra", "L&T", "Larsen & Toubro", "artillery", "howitzer", "Indian Army"],
    target_entity="LT",
    max_pages=1,          # just the article itself — small, polite test
    max_depth=0,
    capture=["html", "text", "images", "screenshot"],
)

seed = load_seed()
matcher = build_matcher(seed)
client = CollectingIngestClient()
history = CrawlHistory(":memory:")

print(f"LIVE crawling: {URL}\n")
result = run_job(job, client, seed, history, matcher)

print("HARVEST:")
print(f"  fetched={result.fetched}  errors={result.errors}  "
      f"dropped_by_gate={result.dropped_by_gate}  kept={result.kept}")
print(f"  gate_reasons={result.gate_reasons}")

if not result.documents:
    # Diagnose: fetch directly to see what the site returned.
    print("\nNo document kept — diagnosing raw fetch:")
    f = Fetcher(user_agent=seed.capture_defaults["user_agent"], timeout_s=30, delay_s=0)
    r = f.fetch(URL)
    print(f"  status={r.status} kind={r.kind} error={r.error} "
          f"bytes={len(r.text_html or '')} robots_ua={seed.capture_defaults['user_agent']}")
    sys.exit(0)

doc = result.documents[0]
print("\nDOCUMENT:")
print(f"  url:           {doc.url}")
print(f"  title:         {doc.title}")
print(f"  source_id:     {doc.source_id} (tier {doc.source_tier})")
print(f"  language:      {doc.language}")
print(f"  published_at:  {doc.published_at}  ({doc.date_precision})")
print(f"  content_hash:  {doc.content_hash[:30]}...")
print(f"  main_text:     {len(doc.main_text)} chars")
print(f"  html:          {len(doc.html)} chars")
print(f"  images kept:   {len(doc.images)}  | screenshot: {bool(doc.screenshot)}")
print(f"  entities:      {[(e.surface, e.resolved_id, e.type) for e in doc.entities_detected][:8]}")
print(f"  stream:        {doc.stream}  | competitor: {doc.detected_competitor}")
print(f"  countries:     {doc.detected_countries}  | tech: {doc.detected_tech_domains}")
print("\n  main_text preview:")
print(textwrap.fill(doc.main_text[:600], width=92,
                    initial_indent="    ", subsequent_indent="    "))

print(f"\nSENT ({result.sent} page bundle(s)):")
for c in client.collected:
    print(f"  - {c['document_id']:20} accepted={c['accepted']} "
          f"failing_rule={c['failing_rule']}")
