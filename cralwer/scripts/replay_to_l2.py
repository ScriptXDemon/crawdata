"""Replay ingested.ndjson records to an L2 Data Engine.

Usage:
  python replay_to_l2.py [--base-url http://192.168.5.153:8000] [--ndjson data/output/ingested.ndjson]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx


def main() -> int:
    p = argparse.ArgumentParser(description="Replay L1 ingested.ndjson to L2")
    p.add_argument("--base-url", default="http://192.168.5.153:8000")
    p.add_argument("--ndjson", default="data/output/ingested.ndjson")
    args = p.parse_args()

    path = Path(args.ndjson)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 1

    base = args.base_url.rstrip("/")
    raw = path.read_text(encoding="utf-8-sig")
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    print(f"Replaying {len(lines)} page bundles to {base}/ingest/v1/page ...")

    ok = fail = 0
    with httpx.Client(timeout=30) as c:
        for i, line in enumerate(lines, 1):
            try:
                rec = json.loads(line)
                body = {"document": rec["document"]}
                resp = c.post(f"{base}/ingest/v1/page", json=body)
                url = rec["document"].get("url", "?")
                if resp.status_code == 200:
                    ok += 1
                    print(f"  [{i}/{len(lines)}] OK {url}")
                else:
                    fail += 1
                    detail = resp.json().get("detail", resp.text[:100]) if resp.text else resp.status_code
                    print(f"  [{i}/{len(lines)}] FAIL {url} - {detail}")
            except Exception as e:
                fail += 1
                print(f"  [{i}/{len(lines)}] FAIL: {e}")

    print(f"\nDone: {ok} accepted, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
