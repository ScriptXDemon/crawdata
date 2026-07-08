"""The L2 → L3 serving DTOs returned by the read-only Serving API.

These mirror the ``srv_*`` tables. Everything is already scored, ranked, and "vs KSSL"; the
client renders them directly.
"""

from __future__ import annotations

import datetime as dt
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SignalCard(ORMModel):
    id: int
    pillar: str
    dir: str
    rank: int
    rank_group: str | None = None
    title: str
    meta: str | None = None
    company: str | None = None
    lens: str | None = None
    sowhat: str | None = None
    tags: list | None = None
    ago_display: str | None = None
    source_url: str | None = None
    provenance: str = "sourced"
    # Trust badges (Phase 1)
    confidence: int | None = None
    confidence_band: str | None = None
    corroboration: int = 1


class SignalDetail(ORMModel):
    signal_id: int
    rank_display: str | None = None
    dir: str | None = None
    title: str
    facts: list | None = None
    what_text: str | None = None
    why_text: str | None = None
    lens_reads: list | None = None
    actions: list | None = None
    suggest: list | None = None
    source_url: str | None = None


class TenderMatch(ORMModel):
    kssl_product_id: str | None = None
    kssl_product_name: str
    fit_level: str
    fit_pct: int
    match_lines: list | None = None


class TenderCard(ORMModel):
    id: int
    title: str
    issuer: str | None = None
    country: str | None = None
    category: str | None = None
    value_display: str | None = None
    qty: str | None = None
    deadline_date: dt.date | None = None
    dl_days: int | None = None
    req_note: str | None = None
    requirements: list | None = None
    lean: str | None = None
    lean_text: str | None = None
    status: str | None = None
    source_url: str | None = None
    provenance: str = "sourced"
    matches: list[TenderMatch] = []


class OverviewMetrics(ORMModel):
    pillar: str
    generated_at: dt.datetime
    metrics: list


class Page(BaseModel, Generic[T]):
    items: list[T]
    page: int
    size: int
    total: int


class MatchupSpec(ORMModel):
    spec_label: str
    comp_value: str | None = None
    kssl_value: str | None = None
    leader: str


class MatchupCard(ORMModel):
    id: int
    category: str | None = None
    dir: str | None = None
    country: str | None = None
    comp_name: str
    comp_by: str | None = None
    kssl_name: str
    edge_score: int
    adv_comp: list | None = None
    adv_kssl: list | None = None
    verdict: str | None = None
    specs: list[MatchupSpec] = []


class GeoEntry(ORMModel):
    id: int
    competitor_id: str | None = None
    competitor_name: str | None = None
    country: str | None = None
    product_name: str | None = None
    category: str | None = None
    contract_value: str | None = None
    since_year: str | None = None
    qty: str | None = None
    stage: str | None = None
    note: str | None = None
    provenance: str = "sourced"
    source_url: str | None = None


class PartnershipCard(ORMModel):
    id: int
    competitor_id: str | None = None
    competitor_name: str | None = None
    partner_name: str
    partner_kind: str | None = None
    rel_type: str | None = None
    country: str | None = None
    deal_value: str | None = None
    date_announced: dt.date | None = None
    kssl_relevance: str | None = None
    meaning: str | None = None
    provenance: str = "sourced"
    source_url: str | None = None


class InnovationCard(ORMModel):
    id: int
    tech_domain_id: str | None = None
    title: str
    maturity: str | None = None
    gap_vs_kssl: str | None = None
    driver: str | None = None
    horizon: str | None = None
    body: str | None = None
    impact: str | None = None
    action: str | None = None
    provenance: str = "sourced"
    source_url: str | None = None


class PatentCard(ORMModel):
    id: str
    competitor_id: str | None = None
    tech_domain_id: str | None = None
    jurisdiction: str | None = None
    title: str
    status: str | None = None
    filed_date: str | None = None
    assignee: str | None = None
    abstract: str | None = None
    kssl_relevance: str | None = None
    provenance: str = "estimate"


class CompetitorSynthesisDTO(ORMModel):
    competitor_id: str
    competitor_name: str | None = None
    thesis: str | None = None
    strat_sowhat: str | None = None
    vulnerabilities: list | None = None
    predictions: list | None = None
    moves: list | None = None
    provenance: str = "estimate"


class FieldPatternDTO(ORMModel):
    id: int
    title: str
    summary: str | None = None
    exceptions: str | None = None
    ord: int = 0


class MalloryRequest(BaseModel):
    message: str
    panel_context: str = "overview"  # overview|signal|tender|matchup|competitor
    entity_id: str | None = None


class MalloryResponse(BaseModel):
    answer: str
    scope: str
    sources: list[str] = []


class ReportRequest(BaseModel):
    focus: str | None = None


class ReportResponse(BaseModel):
    title: str
    generated_at: dt.datetime
    sections: list  # [{heading, body}]


# ── Explainability ("why this?") ──


class EvidenceRef(BaseModel):
    eid: str
    quote: str | None = None
    source_url: str | None = None
    source_tier: int | None = None
    published_at: dt.datetime | None = None
    method: str = "rule"  # rule|llm|llm_verified


class FieldExplanation(BaseModel):
    field: str
    method: str  # rule|llm|llm_verified
    evidence: list[EvidenceRef] = []


class ExplainResponse(BaseModel):
    target_kind: str
    target_id: str
    provenance: str = "sourced"
    confidence: int | None = None
    confidence_band: str | None = None
    confidence_parts: list | None = None
    evidence_count: int = 0
    fields: list[FieldExplanation] = []
