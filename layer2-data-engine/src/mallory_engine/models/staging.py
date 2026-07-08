"""Staging (``stg_*``) models — what the Ingest API writes from crawler records.

Append-only, never read by the client. Each row walks a processing state machine:
``received → resolved → classified → enriched → published``.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


class StgDocument(Base):
    __tablename__ = "stg_documents"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # 'doc_8a91'
    url: Mapped[str] = mapped_column(String, unique=True)  # canonical; dedup key
    content_hash: Mapped[str] = mapped_column(String, index=True)  # dedup key
    source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    author: Mapped[str | None] = mapped_column(String, nullable=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    date_precision: Mapped[str | None] = mapped_column(String, nullable=True)
    language: Mapped[str | None] = mapped_column(String, nullable=True)
    access: Mapped[str | None] = mapped_column(String, nullable=True)
    main_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    main_text_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    images: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    attachments: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    screenshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    tables: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    entities_detected: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    dedup_status: Mapped[str | None] = mapped_column(String, nullable=True)  # new|duplicate_of:<id>
    # When L1 sends a bare document (its normal mode), L2's extraction stage derives the
    # typed records; this stamps that it ran (idempotency guard for the scheduler).
    extracted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StgSignal(Base):
    __tablename__ = "stg_signals"
    id: Mapped[int] = mapped_column(  # BigInteger→INTEGER on SQLite so autoincrement works
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    document_id: Mapped[str] = mapped_column(ForeignKey("stg_documents.id"))
    # Crawler-supplied
    stream: Mapped[str] = mapped_column(String)  # competitive|market|technology
    competitor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    detected_products: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    detected_country: Mapped[str | None] = mapped_column(String, nullable=True)
    tech_domain: Mapped[str | None] = mapped_column(String, nullable=True)
    event_summary: Mapped[str] = mapped_column(Text)
    deal_value_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    deal_value_num: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    deal_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # L2-computed
    resolved_competitor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    dir: Mapped[str | None] = mapped_column(String, nullable=True)  # threat|watch|fav
    lens: Mapped[str | None] = mapped_column(String, nullable=True)
    tags: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    dedup_group: Mapped[str | None] = mapped_column(String, nullable=True)
    proc_status: Mapped[str] = mapped_column(String, default="received", index=True)


class StgTender(Base):
    __tablename__ = "stg_tenders"
    id: Mapped[int] = mapped_column(  # BigInteger→INTEGER on SQLite so autoincrement works
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    document_id: Mapped[str] = mapped_column(ForeignKey("stg_documents.id"))
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String)
    issuer: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    category_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    value_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    value_num: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    value_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    qty_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    deadline_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    requirement_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    requirement_fields: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # L2-computed
    value_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    category_id: Mapped[str | None] = mapped_column(String, nullable=True)
    proc_status: Mapped[str] = mapped_column(String, default="received", index=True)


class StgPartnership(Base):
    __tablename__ = "stg_partnerships"
    id: Mapped[int] = mapped_column(  # BigInteger→INTEGER on SQLite so autoincrement works
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    document_id: Mapped[str] = mapped_column(ForeignKey("stg_documents.id"))
    competitor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    partner_name: Mapped[str] = mapped_column(String)
    partner_id: Mapped[str | None] = mapped_column(String, nullable=True)
    partner_country: Mapped[str | None] = mapped_column(String, nullable=True)
    partner_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    rel_type: Mapped[str | None] = mapped_column(String, nullable=True)
    ptype_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    deal_value_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    date_announced: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_lines: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    kssl_relevance: Mapped[str | None] = mapped_column(String, nullable=True)
    proc_status: Mapped[str] = mapped_column(String, default="received", index=True)


class StgGeo(Base):
    __tablename__ = "stg_geo"
    id: Mapped[int] = mapped_column(  # BigInteger→INTEGER on SQLite so autoincrement works
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    document_id: Mapped[str] = mapped_column(ForeignKey("stg_documents.id"))
    competitor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    product_name: Mapped[str | None] = mapped_column(String, nullable=True)
    product_id: Mapped[str | None] = mapped_column(String, nullable=True)
    product_category: Mapped[str | None] = mapped_column(String, nullable=True)
    contract_value_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    qty_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    since_year: Mapped[str | None] = mapped_column(String, nullable=True)
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String, nullable=True)
    proc_status: Mapped[str] = mapped_column(String, default="received", index=True)


class StgInnovation(Base):
    __tablename__ = "stg_innovation"
    id: Mapped[int] = mapped_column(  # BigInteger→INTEGER on SQLite so autoincrement works
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    document_id: Mapped[str] = mapped_column(ForeignKey("stg_documents.id"))
    tech_domain: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String)
    competitor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    driver: Mapped[str | None] = mapped_column(String, nullable=True)
    maturity_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    horizon_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    proc_status: Mapped[str] = mapped_column(String, default="received", index=True)


class StgAssetAnalysis(Base):
    """Multimodal analysis of a document's captured assets (images/PDFs/screenshots).

    One row per analyzed asset. ``method`` records how it was produced (vision_llm caption,
    pdf_text spec extraction) so /explain stays honest about model-vs-rule provenance.
    """

    __tablename__ = "stg_asset_analysis"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    document_id: Mapped[str] = mapped_column(ForeignKey("stg_documents.id"), index=True)
    asset_kind: Mapped[str] = mapped_column(String)  # image|pdf|screenshot
    asset_index: Mapped[int] = mapped_column(Integer, default=0)  # nth asset of its kind
    storage_path: Mapped[str | None] = mapped_column(String, nullable=True)
    method: Mapped[str] = mapped_column(String)  # vision_llm|pdf_text
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    labels: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # recognised systems/entities
    extracted_specs: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{label,value}]
    status: Mapped[str] = mapped_column(String, default="ok")  # ok|empty|error
    llm_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StgCompanyEvent(Base):
    __tablename__ = "stg_company_events"
    id: Mapped[int] = mapped_column(  # BigInteger→INTEGER on SQLite so autoincrement works
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    document_id: Mapped[str] = mapped_column(ForeignKey("stg_documents.id"))
    competitor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_type: Mapped[str | None] = mapped_column(String, nullable=True)
    headline: Mapped[str] = mapped_column(String)
    deal_value_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    date_of_event: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_lines: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    proc_status: Mapped[str] = mapped_column(String, default="received", index=True)
