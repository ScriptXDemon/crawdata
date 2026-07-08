"""The L1 → L2 ingestion contract.

These models are the single source of truth for what the crawler must POST. FastAPI validates
every request body against them, so a malformed record is rejected with HTTP 422 before it can
reach staging. Field names and enums mirror ``docs/01_CRAWLER_CONTRACT.md`` §3–§5.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field

# ─── Document sub-objects ─────────────────────────────────────────────────────


class ImageIn(BaseModel):
    url: str
    storage_path: str | None = None
    caption: str | None = None
    role: str | None = None  # product|event|chart|person|map|other
    width: int | None = None
    height: int | None = None


class AttachmentIn(BaseModel):
    url: str
    storage_path: str | None = None
    type: str | None = None  # pdf|xlsx|docx
    extracted_text: str | None = None


class ScreenshotIn(BaseModel):
    storage_path: str
    captured_at: dt.datetime | None = None


class TableIn(BaseModel):
    title: str | None = None
    rows: list[dict] = Field(default_factory=list)


class EntityDetectedIn(BaseModel):
    surface: str
    resolved_id: str | None = None
    type: str | None = None  # competitor|product|country|partner|unknown_company
    confidence: float | None = None


class DocumentIn(BaseModel):
    """The source — one per kept URL. ``main_text`` is required: L2 runs NLP on it."""

    url: str
    content_hash: str
    fetched_at: dt.datetime
    source_id: str
    title: str
    main_text: str
    published_at: dt.datetime | None = None
    source_tier: int | None = None
    author: str | None = None
    date_precision: Literal["exact", "approx", "unknown"] | None = None
    language: str | None = None
    access: Literal["open", "paywalled", "partial"] | None = None
    main_text_en: str | None = None
    summary: str | None = None
    images: list[ImageIn] = Field(default_factory=list)
    attachments: list[AttachmentIn] = Field(default_factory=list)
    screenshot: ScreenshotIn | None = None
    tables: list[TableIn] = Field(default_factory=list)
    entities_detected: list[EntityDetectedIn] = Field(default_factory=list)


# ─── The six typed records (crawler-supplied fields only) ─────────────────────


class CompetitiveSignalIn(BaseModel):
    document_id: str | None = None
    stream: Literal["competitive", "market", "technology"]
    competitor_id: str | None = None
    detected_products: list[str] = Field(default_factory=list)
    detected_country: str | None = None
    tech_domain: str | None = None
    event_summary: str
    deal_value_raw: str | None = None
    deal_value_num: float | None = None
    deal_currency: str | None = None
    published_at: dt.datetime | None = None


class ReqFieldIn(BaseModel):
    label: str
    value: str


class TenderIn(BaseModel):
    document_id: str | None = None
    source_ref: str | None = None
    title: str
    issuer: str | None = None
    country: str | None = None
    category_hint: str | None = None
    value_raw: str | None = None
    value_num: float | None = None
    value_currency: str | None = None
    qty_raw: str | None = None
    deadline_date: dt.date | None = None
    requirement_text: str | None = None
    requirement_fields: list[ReqFieldIn] = Field(default_factory=list)


class PartnershipIn(BaseModel):
    document_id: str | None = None
    competitor_id: str | None = None
    partner_name: str
    partner_id: str | None = None
    partner_country: str | None = None
    partner_kind: str | None = None
    rel_type: Literal["jv", "mou", "license", "supply", "customer", "investment"] | None = None
    ptype_raw: str | None = None
    deal_value_raw: str | None = None
    date_announced: dt.date | None = None
    description: str | None = None
    detected_lines: list[str] = Field(default_factory=list)


class GeoFootprintIn(BaseModel):
    document_id: str | None = None
    competitor_id: str | None = None
    country: str | None = None
    product_name: str | None = None
    product_id: str | None = None
    product_category: str | None = None
    contract_value_raw: str | None = None
    qty_raw: str | None = None
    since_year: str | None = None
    stage: Literal["Offered", "Trials", "Contracted", "Delivered"] | None = None
    note: str | None = None
    confidence: Literal["high", "medium", "low"] | None = None


class InnovationIn(BaseModel):
    document_id: str | None = None
    tech_domain: str | None = None
    title: str
    competitor_id: str | None = None
    driver: str | None = None
    maturity_hint: Literal["concept", "dev", "test", "ioc", "foc"] | None = None
    horizon_hint: str | None = None
    description: str | None = None


class CompanyEventIn(BaseModel):
    document_id: str | None = None
    competitor_id: str | None = None
    event_type: (
        Literal["acquisition", "financial", "leadership", "contract_win", "product_launch"] | None
    ) = None
    headline: str
    deal_value_raw: str | None = None
    date_of_event: dt.date | None = None
    description: str | None = None
    detected_lines: list[str] = Field(default_factory=list)


# ─── One-shot page envelope (recommended: 1 document + N records atomically) ───


class PageEnvelopeIn(BaseModel):
    document: DocumentIn
    signals: list[CompetitiveSignalIn] = Field(default_factory=list)
    tenders: list[TenderIn] = Field(default_factory=list)
    partnerships: list[PartnershipIn] = Field(default_factory=list)
    geo: list[GeoFootprintIn] = Field(default_factory=list)
    innovation: list[InnovationIn] = Field(default_factory=list)
    company_events: list[CompanyEventIn] = Field(default_factory=list)
