"""Smoke driver: run a real crawler record through the configured LLM provider.

    # offline (deterministic stub)
    python -m mallory_engine.scripts.try_llm

    # live local Ollama (needs `ollama serve` + qwen2.5:14b)
    LLM_PROVIDER=ollama python -m mallory_engine.scripts.try_llm

Reads the first document from the crawler output NDJSON, classifies + enriches its title as a
competitive signal, and prints the result. With a DB reachable it also shows the llm_runs
ledger and demonstrates a cache hit on the second call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..config import get_settings
from ..services.llm import get_llm

NDJSON = Path(__file__).resolve().parents[4] / "cralwer" / "data" / "output" / "ingested.ndjson"


def _first_event() -> tuple[str, str]:
    """Return (title, value_hint) from the first crawler record, or a built-in sample."""
    if NDJSON.exists():
        for line in NDJSON.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            title = rec.get("document", {}).get("title") or rec.get("title")
            if title:
                return title, ""
    return "L&T secures Rs 4,500 cr K9 Vajra follow-on order", "Rs 4,500 cr"


def _try_db():
    from sqlalchemy import text
    try:
        from ..db import SessionLocal
        db = SessionLocal()
        db.execute(text("SELECT 1"))  # actually open a connection
        return db
    except Exception as exc:  # no Postgres → run without ledger
        print(f"[no db: {type(exc).__name__}] running without cache/ledger")
        return None


def main() -> None:
    # Windows consoles default to cp1252 and choke on ₹/₪/é in real defense data.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = get_settings()
    title, value_hint = _first_event()
    db = _try_db()
    llm = get_llm(settings, db=db)

    print(f"provider={settings.llm_provider} model_fast={settings.ollama_model_fast} "
          f"model_deep={settings.ollama_model_deep}")
    print(f"event: {title!r}\n")

    cls = llm.classify_signal(stream="competitive", event_summary=title, threat_level=None)
    print("classify_signal ->", json.dumps(cls, ensure_ascii=False))

    facts = [["Company", "L&T"], ["Domain", "Artillery"]]
    if value_hint:
        facts.append(["Value", value_hint])
    enr = llm.enrich_signal(stream="competitive", event_summary=title, company="L&T",
                            dir=cls["dir"], facts=facts)
    print(f"\nsowhat -> {enr['sowhat']}")
    print(f"why    -> {enr['why_text'][:200]}")

    if db is not None:
        db.commit()
        from ..models.llm_ops import LlmRun
        from sqlalchemy import select, func
        n = db.scalar(select(func.count()).select_from(LlmRun))
        print(f"\nllm_runs ledger rows: {n}")
        # second identical call should hit cache (no new ledger row for classify)
        llm.classify_signal(stream="competitive", event_summary=title, threat_level=None)
        db.commit()
        n2 = db.scalar(select(func.count()).select_from(LlmRun))
        print(f"after repeat classify: {n2} rows "
              f"({'cache hit' if n2 == n else 'no cache - new row'})")
        db.close()


if __name__ == "__main__":
    main()
