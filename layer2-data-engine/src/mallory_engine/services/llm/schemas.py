"""Pydantic output schemas + JSON-schema exports for structured generation.

The transport asks the model to fill these; ``tasks._run_structured`` validates the raw text
against them (one retry on failure) before it ever reaches the pipeline. Schemas mirror the
dict shapes the existing ``LLMProvider`` methods already return, so downstream code is unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClassifyOut(BaseModel):
    dir: str = Field(pattern="^(threat|watch|fav)$")
    lens: str
    tags: list[str] = Field(default_factory=list)


class LensOut(BaseModel):
    lens: str


class EnrichOut(BaseModel):
    sowhat: str = Field(max_length=400)
    what_text: str
    why_text: str
    lens_reads: list[list[str]] = Field(default_factory=list)
    actions: list[list[str]] = Field(default_factory=list)
    suggest: list[str] = Field(default_factory=list)


class TenderVerdictOut(BaseModel):
    lean: str = Field(pattern="^(go|maybe|pass)$")
    lean_text: str


# ── Extraction (replaces regex; 7b fast model) ──


class ExtractSignal(BaseModel):
    """The one signal every kept page yields. stream + summary always present."""

    stream: str = Field(pattern="^(competitive|market|technology)$")
    competitor_id: str | None = None   # a KNOWN ref id or null — never invent one
    products: list[str] = Field(default_factory=list)
    country: str | None = None
    tech_domain: str | None = None
    summary: str                        # one-line event summary (the signal title)
    deal_value: str | None = None       # raw string as written, e.g. "$254M", "₹4,500 cr"


class ExtractTender(BaseModel):
    title: str
    country: str | None = None
    category: str | None = None
    value: str | None = None
    deadline_days: int | None = None    # "closing in N days" → N, else null


class ExtractPartnership(BaseModel):
    competitor_id: str | None = None
    partner_name: str
    rel_type: str | None = None         # jv|license|mou|supply|investment
    value: str | None = None


class ExtractGeo(BaseModel):
    competitor_id: str | None = None
    country: str
    product: str | None = None
    value: str | None = None
    stage: str | None = None            # Contracted|Offered


class ExtractEvent(BaseModel):
    competitor_id: str | None = None
    event_type: str | None = None       # acquisition|investment|leadership
    headline: str
    value: str | None = None


class ExtractOut(BaseModel):
    """One page → its typed records. signal always; the rest only when the page supports them."""

    signal: ExtractSignal
    tender: ExtractTender | None = None
    partnership: ExtractPartnership | None = None
    geo: ExtractGeo | None = None
    event: ExtractEvent | None = None


# ── Multimodal (Phase B; vision + fast models) ──


class CaptionOut(BaseModel):
    caption: str = Field(max_length=400)
    labels: list[str] = Field(default_factory=list)  # recognised systems/entities; [] if unsure


class SpecRow(BaseModel):
    label: str
    value: str


class ExtractSpecsOut(BaseModel):
    specs: list[SpecRow] = Field(default_factory=list)


# ── Synthesis engines (S-22/23/24) ──


class Vulnerability(BaseModel):
    title: str = Field(max_length=120)
    intel: str = Field(max_length=500)
    cites: list[str] = Field(default_factory=list)


class SynthesisOut(BaseModel):
    """S-23 output — mirrors the seed/serving shape exactly."""

    thesis: str = Field(max_length=500)
    strat_sowhat: str = Field(max_length=500)
    vulnerabilities: list[Vulnerability] = Field(min_length=1)
    predictions: list[str] = Field(default_factory=list)
    moves: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)  # legal escape hatch — declared, not invented
    cites: list[str] = Field(min_length=1)  # evidence ids the whole synthesis rests on


class MatchupVerdictOut(BaseModel):
    verdict: str = Field(max_length=400)


class FieldPatternOut(BaseModel):
    title: str = Field(max_length=120)
    summary: str = Field(max_length=500)
    exceptions: str = ""
    bottom_line: str = ""
    cites: list[str] = Field(default_factory=list)


class FieldPatternsOut(BaseModel):
    patterns: list[FieldPatternOut] = Field(min_length=1, max_length=6)


# JSON Schemas passed to the transport's response_format. Pydantic generates them;
# we strip the title noise Ollama doesn't need.
def _schema(model: type[BaseModel]) -> dict:
    s = model.model_json_schema()
    s.pop("title", None)
    return s


CLASSIFY_SCHEMA = _schema(ClassifyOut)
LENS_SCHEMA = _schema(LensOut)
ENRICH_SCHEMA = _schema(EnrichOut)
TENDER_VERDICT_SCHEMA = _schema(TenderVerdictOut)
SYNTHESIS_SCHEMA = _schema(SynthesisOut)
MATCHUP_VERDICT_SCHEMA = _schema(MatchupVerdictOut)
FIELD_PATTERNS_SCHEMA = _schema(FieldPatternsOut)
EXTRACT_SCHEMA = _schema(ExtractOut)
CAPTION_SCHEMA = _schema(CaptionOut)
EXTRACT_SPECS_SCHEMA = _schema(ExtractSpecsOut)
