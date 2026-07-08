"""Live eval — grades the configured Ollama model against golden properties.

Skipped unless MALLORY_EVAL_LIVE=1 and LLM_PROVIDER=ollama with a reachable server. Emits a
scorecard (per-task pass rate + latency) — the tool for assigning fast/deep roles when the
remote door opens with unknown models.
"""

from __future__ import annotations

import os
import time

import pytest

from mallory_engine.config import get_settings
from mallory_engine.services.llm import get_llm

from .properties import load_cases, run_case

pytestmark = pytest.mark.live

RUN_LIVE = os.environ.get("MALLORY_EVAL_LIVE") == "1"


@pytest.mark.skipif(not RUN_LIVE, reason="set MALLORY_EVAL_LIVE=1 to run live eval")
def test_live_scorecard() -> None:
    settings = get_settings()
    llm = get_llm(settings)
    cases = load_cases()

    passed = 0
    per_task: dict[str, list[int]] = {}
    total_ms = 0.0
    print(f"\n=== LIVE EVAL — provider={settings.llm_provider} "
          f"fast={settings.ollama_model_fast} deep={settings.ollama_model_deep} ===")
    for case in cases:
        t0 = time.perf_counter()
        fails = run_case(llm, case)
        ms = (time.perf_counter() - t0) * 1000
        total_ms += ms
        ok = not fails
        passed += ok
        per_task.setdefault(case["task"], []).append(1 if ok else 0)
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {case['task']:16s} {ms:6.0f}ms  {case.get('note', '')[:40]}"
              + ("" if ok else f"  -> {fails}"))

    print("  --- per task ---")
    for task, results in per_task.items():
        print(f"    {task:16s} {sum(results)}/{len(results)}")
    print(f"  TOTAL {passed}/{len(cases)}  avg {total_ms / max(len(cases), 1):.0f}ms/case")

    # Live models are graded, not gated — the scorecard is the artifact. Require only that
    # the run completed and a majority of cases pass (catches a broken model/endpoint).
    assert passed >= len(cases) // 2, f"live model passed only {passed}/{len(cases)}"
