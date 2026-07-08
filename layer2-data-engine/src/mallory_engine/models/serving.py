"""Serving (``srv_*``) models — the ONLY tables the Layer 3 client reads.

Denormalized and pre-computed: every value the UI shows is a literal column here, so the
client never computes a score, rank, or color.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, Date, DateTime, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


class SrvSignal(Base):
    """One row = one card in an overview feed (competitive / market / technology)."""

    __tablename__ = "srv_signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pillar: Mapped[str] = mapped_column(String, index=True)  # competitive|market|technology
    dir: Mapped[str] = mapped_column(String)  # threat|watch|fav (pre-computed color)
    rank: Mapped[int] = mapped_column(Integer)  # pre-sorted; client just ORDER BY rank
    rank_group: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String)
    meta: Mapped[str | None] = mapped_column(String, nullable=True)
    company: Mapped[str | None] = mapped_column(String, nullable=True)
    lens: Mapped[str | None] = mapped_column(String, nullable=True)
    sowhat: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    ago_display: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default="sourced")  # sourced|estimate
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Trust spine (Phase 1) — all deterministic, decomposable in confidence_parts.
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-100
    confidence_band: Mapped[str | None] = mapped_column(String, nullable=True)  # high|medium|low
    confidence_parts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    corroboration: Mapped[int] = mapped_column(Integer, default=1)  # independent sources


class SrvEvidence(Base):
    """Evidence chain: every srv_* field → the exact source rows that back it.

    Written by *every* publish path (rule- and LLM-produced), so explainability is uniform.
    ``llm_run_id`` is NULL for rule-produced fields; ``method`` records how it was made.
    """

    __tablename__ = "srv_evidence"
    # BigInteger→INTEGER on SQLite so autoincrement works there (Postgres uses BIGSERIAL).
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    target_kind: Mapped[str] = mapped_column(String, index=True)  # signal|tender|partnership|...
    target_id: Mapped[str] = mapped_column(String, index=True)  # srv pk (as text)
    field: Mapped[str] = mapped_column(String)  # 'sowhat' | 'card' | 'vulnerability:0' | ...
    evidence_id: Mapped[str] = mapped_column(String)  # eid: 'doc:doc_8a91' | 'sig:412' | ...
    quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    source_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    method: Mapped[str] = mapped_column(String, default="rule")  # rule|llm|llm_verified
    llm_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class SrvSignalDetail(Base):
    """Right-panel detail, 1:1 with srv_signals."""

    __tablename__ = "srv_signal_details"
    signal_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rank_display: Mapped[str | None] = mapped_column(String, nullable=True)
    dir: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String)
    facts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    what_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    why_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    lens_reads: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    actions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    suggest: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)


class SrvTender(Base):
    __tablename__ = "srv_tenders"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    issuer: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    value_display: Mapped[str | None] = mapped_column(String, nullable=True)
    value_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    qty: Mapped[str | None] = mapped_column(String, nullable=True)
    deadline_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    dl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)  # recomputed daily
    req_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    requirements: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    lean: Mapped[str | None] = mapped_column(String, nullable=True)  # go|maybe|pass
    lean_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)  # open|closing|closed
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default="sourced")


class SrvTenderMatch(Base):
    __tablename__ = "srv_tender_matches"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    tender_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kssl_product_id: Mapped[str | None] = mapped_column(String, nullable=True)
    kssl_product_name: Mapped[str] = mapped_column(String)
    fit_level: Mapped[str] = mapped_column(String)  # high|medium|low
    fit_pct: Mapped[int] = mapped_column(Integer)
    match_lines: Mapped[list | None] = mapped_column(JSONB, nullable=True)


class SrvOverviewMetrics(Base):
    """Pre-computed metric strip for each overview header (client renders zero math)."""

    __tablename__ = "srv_overview_metrics"
    pillar: Mapped[str] = mapped_column(String, primary_key=True)
    generated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[list] = mapped_column(JSONB)


class SrvMatchup(Base):
    """Positioning: one KSSL product benchmarked against one competitor product."""

    __tablename__ = "srv_matchups"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    category: Mapped[str | None] = mapped_column(String, index=True)
    dir: Mapped[str | None] = mapped_column(String)  # threat|watch|fav (who leads)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    comp_name: Mapped[str] = mapped_column(String)
    comp_by: Mapped[str | None] = mapped_column(String, nullable=True)
    kssl_name: Mapped[str] = mapped_column(String)
    edge_score: Mapped[int] = mapped_column(Integer)  # 0-100, KSSL's edge
    adv_comp: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    adv_kssl: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    edge_parts: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # per-spec contribution
    provenance: Mapped[str] = mapped_column(String, default="estimate")
    verdict_method: Mapped[str] = mapped_column(String, default="rule")  # rule|llm


class SrvMatchupSpec(Base):
    __tablename__ = "srv_matchup_specs"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    matchup_id: Mapped[int] = mapped_column(BigInteger, index=True)
    spec_label: Mapped[str] = mapped_column(String)
    comp_value: Mapped[str | None] = mapped_column(String, nullable=True)
    kssl_value: Mapped[str | None] = mapped_column(String, nullable=True)
    leader: Mapped[str] = mapped_column(String)  # comp|kssl|tie


class SrvGeoEntry(Base):
    __tablename__ = "srv_geo_entries"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    competitor_id: Mapped[str | None] = mapped_column(String, index=True)
    competitor_name: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, index=True)
    product_name: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    contract_value: Mapped[str | None] = mapped_column(String, nullable=True)
    since_year: Mapped[str | None] = mapped_column(String, nullable=True)
    qty: Mapped[str | None] = mapped_column(String, nullable=True)
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default="sourced")
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)


class SrvPartnership(Base):
    __tablename__ = "srv_partnerships"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    competitor_id: Mapped[str | None] = mapped_column(String, index=True)
    competitor_name: Mapped[str | None] = mapped_column(String, nullable=True)
    partner_name: Mapped[str] = mapped_column(String)
    partner_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    rel_type: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    deal_value: Mapped[str | None] = mapped_column(String, nullable=True)
    date_announced: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    kssl_relevance: Mapped[str | None] = mapped_column(String)  # CORE|ADJACENT|context
    meaning: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default="sourced")
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)


class SrvInnovation(Base):
    __tablename__ = "srv_innovation"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    tech_domain_id: Mapped[str | None] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    maturity: Mapped[str | None] = mapped_column(String, nullable=True)  # concept|dev|test|ioc|foc
    gap_vs_kssl: Mapped[str | None] = mapped_column(String, nullable=True)  # ahead|parity|behind
    driver: Mapped[str | None] = mapped_column(String, nullable=True)
    horizon: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default="sourced")
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)


class SrvPatent(Base):
    __tablename__ = "srv_patents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    competitor_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    tech_domain_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    jurisdiction: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String, nullable=True)  # granted|pending|filed
    filed_date: Mapped[str | None] = mapped_column(String, nullable=True)
    assignee: Mapped[str | None] = mapped_column(String, nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    kssl_relevance: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default="estimate")


class SrvCompetitorSynthesis(Base):
    __tablename__ = "srv_competitor_synthesis"
    competitor_id: Mapped[str] = mapped_column(String, primary_key=True)
    competitor_name: Mapped[str | None] = mapped_column(String, nullable=True)
    thesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    strat_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    strat_sowhat: Mapped[str | None] = mapped_column(Text, nullable=True)
    vulnerabilities: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{title,intel}]
    predictions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    moves: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default="estimate")
    gaps: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # declared evidence gaps
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence_band: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SrvFieldPattern(Base):
    __tablename__ = "srv_field_patterns"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    title: Mapped[str] = mapped_column(String)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    exceptions: Mapped[str | None] = mapped_column(Text, nullable=True)
    ord: Mapped[int] = mapped_column(Integer, default=0)
    bottom_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default="estimate")
