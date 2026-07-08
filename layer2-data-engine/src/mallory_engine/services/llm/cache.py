"""LLM run cache + ledger writes over the ``llm_runs`` table.

Both operations are best-effort: if no session is supplied or the DB is unreachable, they
no-op so the stub/CI path stays database-free and a ledger failure never breaks a generation.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models.llm_ops import LlmRun


def input_hash(task: str, model: str, payload: dict, *, template_ver: str = "v1") -> str:
    canonical = json.dumps(
        {"task": task, "model": model, "template": template_ver, "payload": payload},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def lookup(db: Session | None, task: str, model: str, ihash: str) -> dict | None:
    if db is None:
        return None
    try:
        # no_autoflush: the ledger read must not force-flush the caller's in-progress rows.
        with db.no_autoflush:
            row = db.scalars(
                select(LlmRun)
                .where(LlmRun.task == task, LlmRun.model == model,
                       LlmRun.input_hash == ihash, LlmRun.status == "ok")
                .order_by(LlmRun.id.desc())
                .limit(1)
            ).first()
            return row.output if row else None
    except Exception:
        return None


def record(db: Session | None, *, task: str, model: str, provider: str, ihash: str,
           evidence_ids: list | None, output: dict | None, validator_results: dict | None,
           status: str, latency_ms: int | None) -> None:
    if db is None:
        return
    run = LlmRun(
        task=task, input_hash=ihash, model=model, provider=provider,
        evidence_ids=evidence_ids, output=output, validator_results=validator_results,
        status=status, latency_ms=latency_ms,
    )
    # Add to the session but DON'T flush here: flushing mid-pipeline can trip autoflush on the
    # caller's half-built rows, and a rollback-on-failure would wipe the caller's work. The
    # outer transaction flushes/commits the ledger row alongside everything else.
    try:
        db.add(run)
    except Exception:
        pass  # ledger is best-effort; never disturb the caller's transaction
