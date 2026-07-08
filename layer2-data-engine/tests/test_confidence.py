"""Confidence scoring is deterministic tradecraft — the trust ordering must always hold."""

from __future__ import annotations

import datetime as dt

from mallory_engine.services.confidence import score

NOW = dt.datetime(2026, 7, 6, tzinfo=dt.timezone.utc)
FRESH = dt.datetime(2026, 7, 5, tzinfo=dt.timezone.utc)
OLD = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)


def _s(**kw):
    kw.setdefault("published_at", FRESH)
    kw.setdefault("provenance", "sourced")
    kw.setdefault("pillar", "competitive")
    kw.setdefault("now", NOW)
    return score(**kw)


def test_corroboration_and_authority_beat_a_lone_aggregator() -> None:
    lone, _, _ = _s(source_tier=3, independent_sources=1)
    strong, band, _ = _s(source_tier=1, independent_sources=3)
    assert strong > lone
    assert band == "high"


def test_staleness_lowers_score() -> None:
    fresh, _, _ = _s(source_tier=1, independent_sources=3, published_at=FRESH)
    stale, _, _ = _s(source_tier=1, independent_sources=3, published_at=OLD, pillar="market")
    assert stale < fresh


def test_estimate_provenance_penalised() -> None:
    sourced, _, _ = _s(source_tier=2, independent_sources=1, provenance="sourced")
    estimate, _, _ = _s(source_tier=2, independent_sources=1, provenance="estimate")
    assert estimate < sourced


def test_score_clamped_and_parts_decompose() -> None:
    total, band, parts = _s(source_tier=1, independent_sources=3)
    assert 5 <= total <= 95
    assert {p["factor"] for p in parts} == {"source", "corroboration", "freshness", "provenance"}
    # unclamped parts sum equals the reported score for this non-extreme case
    assert sum(p["points"] for p in parts) == total


def test_unknown_date_is_neutral_not_zero() -> None:
    known, _, _ = _s(source_tier=2, independent_sources=1, published_at=FRESH)
    unknown, _, parts = _s(source_tier=2, independent_sources=1, published_at=None)
    fresh_part = next(p for p in parts if p["factor"] == "freshness")
    assert 0 < fresh_part["points"] < fresh_part["max"]
