"""Mock crawler — POST sample page envelopes to the Ingest API, then trigger processing.

Lets Layer 2 (and Layer 3) be developed and demoed before the real crawler exists. The sample
records are in the exact crawler-output shape, so the real crawler drops straight in.

Usage:  python -m mallory_engine.scripts.mock_feeder   (API must be running)
Env:    MALLORY_API (default http://localhost:8000), SAMPLE_FILE (default sample_data/sample_records.json)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

API = os.environ.get("MALLORY_API", "http://localhost:8000")
DEFAULT_SAMPLE = Path(__file__).resolve().parents[3] / "sample_data" / "sample_records.json"


def main() -> None:
    sample_file = Path(os.environ.get("SAMPLE_FILE", DEFAULT_SAMPLE))
    envelopes = json.loads(sample_file.read_text())

    with httpx.Client(base_url=API, timeout=30) as client:
        for env in envelopes:
            resp = client.post("/ingest/v1/page", json=env)
            resp.raise_for_status()
            print(f"ingested {env['document']['url']} -> {resp.json()['ingested']}")

        proc = client.post("/ops/process")
        proc.raise_for_status()
        print("pipeline:", proc.json())


if __name__ == "__main__":
    main()
