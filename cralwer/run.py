"""Crawler CLI.

  python run.py testing                 # run the §8 testing batch + exit criteria (offline)
  python run.py gen                      # generate jobs from the seed -> jobs/generated_jobs.json
  python run.py run jobs/foo.json        # run a job file end-to-end (in-process ingest)
  python run.py push jobs/foo.json       # run jobs and POST pages to a real L2 (INGEST_BASE_URL)
  python run.py serve                    # serve the stub Ingest API + dashboard on :9090
  python run.py crawler-api              # serve the Crawler API (job in -> records out) on :8099

Set CRAWLER_ALLOW_NETWORK=1 to allow live fetching (fixtures still take priority
unless CRAWLER_PREFER_FIXTURES=0).
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from crawler import config


def _summary(results) -> None:
    tot = Counter()
    by_type: Counter = Counter()
    for r in results:
        tot["fetched"] += r.fetched
        tot["kept"] += r.kept
        tot["dropped"] += r.dropped_by_gate
        tot["304"] += r.not_modified
        tot["skipped_unchanged"] += r.skipped_unchanged
        tot["skipped_duplicate"] += r.skipped_duplicate
        tot["emitted"] += r.emitted
        tot["accepted"] += r.accepted
        tot["rejected"] += r.rejected
        for o in r.outcomes:
            if o.accepted:
                by_type[o.record_type] += 1
    print("\n=== BATCH SUMMARY ===")
    for k in ("fetched", "kept", "dropped", "304", "skipped_unchanged",
              "skipped_duplicate", "emitted", "accepted", "rejected"):
        print(f"  {k:18} {tot[k]}")
    print(f"  accepted by type   {dict(by_type)}")


def cmd_testing() -> int:
    import run_testing_batch
    return run_testing_batch.main()


def cmd_gen() -> int:
    from crawler.jobgen import distinct_sites, generate, write_jobs
    jobs = generate()
    out = Path("jobs/generated_jobs.json")
    out.parent.mkdir(exist_ok=True)
    write_jobs(jobs, out)
    by = Counter(j.job_type for j in jobs)
    print(f"Generated {len(jobs)} jobs across {len({u for j in jobs for u in j.seed_urls})} "
          f"target URLs / {distinct_sites(jobs)} distinct hosts.")
    print(f"  by type: {dict(by)}")
    print(f"  written -> {out}")
    return 0


def cmd_run(path: str) -> int:
    from crawler.ingest_client import InProcessIngestClient
    from crawler.models import Job
    from crawler.pipeline import run_batch
    raw = json.loads(Path(path).read_text())
    jobs = [Job(**j) for j in raw]
    client = InProcessIngestClient()
    print(f"Running {len(jobs)} jobs from {path} "
          f"(network={'on' if config.allow_network() else 'off'}, "
          f"fixtures={'on' if config.prefer_fixtures() else 'off'})")
    results = run_batch(jobs, client)
    _summary(results)
    print(f"  accepted records -> {config.OUTPUT_DIR / 'ingested.ndjson'}")
    return 0


def cmd_push(path: str) -> int:
    """Run jobs and POST every kept page to a real Layer-2 Ingest API over HTTP.

    Target comes from INGEST_BASE_URL (e.g. http://127.0.0.1:8000 for a local L2).
    """
    from crawler.ingest_client import HttpIngestClient
    from crawler.models import Job
    from crawler.pipeline import run_batch
    raw = json.loads(Path(path).read_text())
    jobs = [Job(**j) for j in raw]
    client = HttpIngestClient()
    print(f"Pushing {len(jobs)} jobs from {path} -> {client.base_url} "
          f"(network={'on' if config.allow_network() else 'off'}, "
          f"fixtures={'on' if config.prefer_fixtures() else 'off'})")
    results = run_batch(jobs, client)

    tot = Counter()
    reasons: Counter = Counter()
    for r in results:
        for k in ("fetched", "kept", "dropped_by_gate", "not_modified",
                  "skipped_unchanged", "skipped_duplicate", "sent", "accepted",
                  "rejected", "errors"):
            tot[k] += getattr(r, k, 0)
        reasons.update(r.gate_reasons)
        for o in r.outcomes:
            if not o.accepted:
                print(f"  REJECTED: {o.failing_rule}")
    print("\n=== PUSH SUMMARY ===")
    for k, v in tot.items():
        print(f"  {k:18} {v}")
    if reasons:
        print(f"  gate reasons       {dict(reasons)}")
    return 0 if tot["errors"] == 0 else 1


def cmd_serve() -> int:
    import uvicorn
    uvicorn.run("ingest_api.app:app", host="0.0.0.0", port=9090, log_level="info")
    return 0


def cmd_crawler_api() -> int:
    import os
    import uvicorn
    port = int(os.environ.get("CRAWLER_API_PORT", "8099"))
    uvicorn.run("crawler_api.app:app", host="0.0.0.0", port=port, log_level="info")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, *rest = argv
    if cmd == "testing":
        return cmd_testing()
    if cmd == "gen":
        return cmd_gen()
    if cmd == "run":
        if not rest:
            print("usage: python run.py run <jobs.json>")
            return 2
        return cmd_run(rest[0])
    if cmd == "push":
        if not rest:
            print("usage: INGEST_BASE_URL=http://127.0.0.1:8000 python run.py push <jobs.json>")
            return 2
        return cmd_push(rest[0])
    if cmd == "serve":
        return cmd_serve()
    if cmd == "crawler-api":
        return cmd_crawler_api()
    print(f"unknown command: {cmd}\n{__doc__}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
