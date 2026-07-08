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
