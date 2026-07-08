"""Control-plane tables: the Source Catalog, the Coverage ledger, and the Job run log."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Source(Base):
    """One row per registrable domain (eTLD+1). Human adds {url, frequency, category};
    everything else (source_id, tier, source_known, accept_rate) is automatic."""

    __tablename__ = "sources"
    domain: Mapped[str] = mapped_column(String, primary_key=True)  # eTLD+1
    source_id: Mapped[str] = mapped_column(String, index=True)
    category: Mapped[str] = mapped_column(String)
    tier: Mapped[int] = mapped_column(Integer)  # 1..3 (confidence weight only, NOT relevance)
    frequency: Mapped[str] = mapped_column(String, default="daily")  # 6h|daily|weekly — crawl cadence
    region: Mapped[str | None] = mapped_column(String, nullable=True)
    source_known: Mapped[bool] = mapped_column(Boolean, default=True)
    tier_origin: Mapped[str] = mapped_column(String, default="human")  # human|heuristic|learned
    added_by: Mapped[str] = mapped_column(String, default="human")  # human|auto
    # Optional explicit crawl targets (used for the offline end-to-end test); production builds
    # search URLs from the seed instead.
    seed_urls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    search_template: Mapped[str | None] = mapped_column(String, nullable=True)  # {q} placeholder
    accept_rate: Mapped[float | None] = mapped_column(nullable=True)
    last_crawled: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class CoverageCell(Base):
    """One (source × entity) cell of the required coverage matrix — the no-miss ledger."""

    __tablename__ = "coverage_cells"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # f"{domain}|{entity_id}"
    domain: Mapped[str] = mapped_column(String, index=True)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    cadence: Mapped[str] = mapped_column(String, default="daily")
    last_fetched: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_emitted: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, default="never")  # never|fresh|stale


class JobRun(Base):
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String, index=True)
    domain: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    dispatched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    kept: Mapped[int] = mapped_column(Integer, default=0)
    records_emitted: Mapped[int] = mapped_column(Integer, default=0)
    records_forwarded: Mapped[int] = mapped_column(Integer, default=0)
    l2_accepted: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="ok")  # ok|error|skipped
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
