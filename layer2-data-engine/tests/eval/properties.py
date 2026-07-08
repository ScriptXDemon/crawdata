"""Property assertions for golden eval cases.

Golden cases assert *properties* of the output (dir, tag presence, no ungrounded numbers,
length bounds), never exact strings — so the same cases grade both the deterministic stub and
a live model without brittle snapshot matching.
"""

from __future__ import annotations

import json
from pathlib import Path

from mallory_engine.services.llm.validators import numbers_grounded

GOLDEN_DIR = Path(__file__).parent / "golden"


def load_cases() -> list[dict]:
    cases: list[dict] = []
    for f in sorted(GOLDEN_DIR.glob("*.json")):
        cases.extend(json.loads(f.read_text(encoding="utf-8")))
    return cases


def run_case(llm, case: dict) -> list[str]:
    """Return a list of property violations (empty = pass)."""
    task, inp, expect = case["task"], case["input"], case["expect"]
    fails: list[str] = []

    if task == "classify_signal":
        out = llm.classify_signal(**inp)
        if "dir" in expect and out["dir"] != expect["dir"]:
            fails.append(f"dir={out['dir']!r} expected {expect['dir']!r}")
        if expect.get("lens_nonempty") and not out.get("lens"):
            fails.append("lens empty")
        for tag in expect.get("tags_include", []):
            if tag not in out.get("tags", []):
                fails.append(f"missing tag {tag!r} in {out.get('tags')}")

    elif task == "enrich_signal":
        out = llm.enrich_signal(**inp)
        sowhat = out.get("sowhat", "")
        if expect.get("sowhat_nonempty") and not sowhat.strip():
            fails.append("sowhat empty")
        mx = expect.get("sowhat_max_len")
        if mx and len(sowhat) > mx:
            fails.append(f"sowhat {len(sowhat)} > {mx}")
        if expect.get("no_uncited_numbers"):
            ev = inp["event_summary"] + " " + " ".join(v for _, v in inp.get("facts", []))
            probs = numbers_grounded(sowhat + " " + out.get("why_text", ""), ev)
            fails.extend(probs)

    elif task == "tender_verdict":
        out = llm.tender_verdict(**inp)
        if "lean" in expect and out["lean"] != expect["lean"]:
            fails.append(f"lean={out['lean']!r} expected {expect['lean']!r}")
        pct = expect.get("lean_text_has_pct")
        if pct is not None and str(pct) not in out.get("lean_text", ""):
            fails.append(f"lean_text missing {pct}%")

    else:
        fails.append(f"unknown task {task}")

    return fails
