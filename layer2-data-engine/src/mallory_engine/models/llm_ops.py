"""LLM operations ledger (``llm_runs``) — cache, idempotency guard, and XAI audit trail.

Every structured LLM call is recorded here: what was asked (task + input hash), which model
answered, the validated output, and whether validators/fallback fired. A cache hit reuses
``output`` for an identical ``(task, input_hash, model)`` so re-processing an unchanged page
costs nothing.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, DateTime, Integer, String
# JSONB on Postgres, plain JSON elsewhere (SQLite in tests) — same Python API.
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base

_JSON = JSON().with_variant(JSONB, "postgresql")


class LlmRun(Base):
    __tablename__ = "llm_runs"

    # BigInteger→INTEGER on SQLite so autoincrement works there (Postgres uses BIGSERIAL).
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    task: Mapped[str] = mapped_column(String, index=True)          # 'classify_signal' | ...
    input_hash: Mapped[str] = mapped_column(String, index=True)    # sha256 of task+template+evidence+params
    model: Mapped[str] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String)
    prompt_template_ver: Mapped[str] = mapped_column(String, default="v1")
    evidence_ids: Mapped[list | None] = mapped_column(_JSON, nullable=True)
    output: Mapped[dict | None] = mapped_column(_JSON, nullable=True)
    validator_results: Mapped[dict | None] = mapped_column(_JSON, nullable=True)
    status: Mapped[str] = mapped_column(String)  # ok | invalid | fallback | error
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
