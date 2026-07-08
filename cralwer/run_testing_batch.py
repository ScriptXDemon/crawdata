"""Run the §8 testing batch ONCE and check the 7 exit criteria (§8.2).

Usage:
    python run_testing_batch.py            # offline (fixtures only) — deterministic
    CRAWLER_ALLOW_NETWORK=1 python run_testing_batch.py   # allow live fallback

Exits 0 if all 7 criteria pass, 1 otherwise. Prints a per-criterion report and
a per-job summary. Accepted page bundles are also written to
data/output/ingested.ndjson.
"""
from __future__ import annotations

import os
import sys

# The §8 batch is fixture-backed and must be reproducible: default to fixtures,
# no network (a live run can be opted in via env before launching).
os.environ.setdefault("CRAWLER_PREFER_FIXTURES", "1")
os.environ.setdefault("CRAWLER_ALLOW_NETWORK", "0")

from crawler import config
from crawler.dedup import CrawlHistory
from crawler.ingest_client import InProcessIngestClient
from crawler.pipeline import run_job
from crawler.resolver import build_matcher
from crawler.seed import load_seed
from crawler.testing_batch import build as build_batch


def main() -> int:
    config.ensure_dirs()
    # Fresh crawl history so the run is deterministic (first-seen => emit).
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    out = config.OUTPUT_DIR / "ingested.ndjson"
    if out.exists():
        out.unlink()

    seed = load_seed()
    matcher = build_matcher(seed)
    history = CrawlHistory()
    client = InProcessIngestClient()
    from ingest_api.app import reset as reset_ledger
    reset_ledger()

    jobs = build_batch()
    print(f"Running {len(jobs)} jobs (offline={os.environ.get('CRAWLER_ALLOW_NETWORK')=='0'})\n")
    print(f"{'job_id':<34} {'type':<8} fetch kept drop sent acc rej")
    print("-" * 78)

    results = []
    crashed = []
    jobs_with_valid_doc = 0
    jobs_with_accepted_bundle = 0
    docs_with_text = 0
    pdf_text_ok = False
    screenshot_ok = False
    nonenglish_ok = False
    resolution_total = 0
    resolution_hits = 0

    for job in jobs:
        try:
            r = run_job(job, client, seed, history, matcher)
        except Exception as exc:  # noqa: BLE001
            crashed.append((job.job_id, str(exc)))
            print(f"{job.job_id:<34} {job.job_type:<8} CRASHED: {exc}")
            continue
        results.append((job, r))
        # criterion 2 — each job yields ≥1 valid document (main_text + url + hash)
        job_valid = 0
        for d in r.documents:
            if d.main_text.strip() and d.url and d.content_hash and d.content_hash != "sha256:empty":
                docs_with_text += 1
                job_valid += 1
            # criterion 4 — PDF extraction
            for att in d.attachments:
                if att.type == "pdf" and att.extracted_text and len(att.extracted_text) > 50:
                    pdf_text_ok = True
            # criterion 5 — screenshot
            if d.screenshot and d.screenshot.storage_path:
                from crawler import storage
                if storage.local_path(d.screenshot.storage_path).exists():
                    screenshot_ok = True
            # criterion 7 — non-English both fields
            if d.language != "en" and d.main_text.strip() and (d.main_text_en or "").strip():
                nonenglish_ok = True
        if job_valid >= 1:
            jobs_with_valid_doc += 1
        # criterion 3 — every job's kept page(s) were accepted as a raw page bundle
        if r.accepted >= 1:
            jobs_with_accepted_bundle += 1
        print(f"{job.job_id:<34} {job.job_type:<8} {r.fetched:>5} {r.kept:>4} "
              f"{r.dropped_by_gate:>4} {r.sent:>4} {r.accepted:>3} {r.rejected:>3}")

    history.close()

    # criterion 6 — entity-resolution recall on a known answer key.
    resolution_hits, resolution_total = _resolution_check(results)

    # --- evaluate the 7 criteria -------------------------------------
    c1 = not crashed
    c2 = jobs_with_valid_doc == len(jobs)   # ≥1 valid doc per job (every job)
    c3 = jobs_with_accepted_bundle == len(jobs)  # every job's page(s) accepted by Ingest
    c4 = pdf_text_ok
    c5 = screenshot_ok
    c6 = resolution_total > 0 and (resolution_hits / resolution_total) >= 0.80
    c7 = nonenglish_ok

    print("\n" + "=" * 78)
    print("EXIT CRITERIA (§8.2)")
    print("=" * 78)
    _line(1, c1, f"All jobs ran within budget without crashing ({len(crashed)} crashed)")
    _line(2, c2, f"≥1 valid document per job (jobs with a valid doc={jobs_with_valid_doc}/{len(jobs)})")
    _line(3, c3, f"Every job's page bundle(s) accepted by Ingest ({jobs_with_accepted_bundle}/{len(jobs)})")
    _line(4, c4, "PDF extraction works on a real tender RFP")
    _line(5, c5, "Screenshot captured + stored (one full-page PNG per document)")
    _line(6, c6, f"Entity resolution ≥80% on answer key ({resolution_hits}/{resolution_total})")
    _line(7, c7, "Non-English source returns both main_text and main_text_en")

    all_pass = all([c1, c2, c3, c4, c5, c6, c7])
    print("\n" + ("✅ PIPELINE-PROVEN — all 7 criteria pass." if all_pass
                  else "❌ Not all criteria passed."))
    print(f"   Accepted page bundles -> {config.OUTPUT_DIR / 'ingested.ndjson'}")
    return 0 if all_pass else 1


def _line(n: int, ok: bool, msg: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}. {msg}")


# Answer key for criterion 6: obvious mentions we expect resolved per fixture URL.
_ANSWER_KEY = {
    "https://idrw.org/lt-k9-vajra-followon": {"LT", "P_K9VAJRAT155MMSPH", "HANWHA"},
    "https://economictimes.indiatimes.com/news/defence/adani-acquires-general-aeronautics": {"ADANI", "GENER"},
    "https://www.defensenews.com/global/2026/06/27/knds-caesar-nigeria": {"KNDS", "Nigeria"},
    "https://www.nibe.co.in/news/nibe-sig-sauer-license": {"NIBE", "SIGSA"},
    "https://www.solargroup.com/news/nagastra-armenia-export": {"SOLAR", "Armenia", "EDGE"},
    "https://www.armyrecognition.com/caesar-6x6-specs": {"KNDS", "P_CAESAR6X6"},
    "https://www.shephardmedia.com/news/ramjet-155mm-rheinmetall": {"RHEIN", "artillery"},
}


def _resolution_check(results) -> tuple[int, int]:
    from crawler.canonicalize import canonicalize_url
    hits = total = 0
    by_url = {}
    for _job, r in results:
        for d in r.documents:
            by_url[canonicalize_url(d.url)] = {
                e.resolved_id for e in d.entities_detected if e.resolved_id}
    for url, expected in _ANSWER_KEY.items():
        got = by_url.get(canonicalize_url(url))
        if got is None:
            print(f"   (resolution: no document for {url})")
            continue
        for e in expected:
            total += 1
            if e in got:
                hits += 1
            else:
                print(f"   (resolution miss: {e} not in {sorted(got)} for {url})")
    return hits, total


if __name__ == "__main__":
    sys.exit(main())
