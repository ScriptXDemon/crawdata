"""Tender scoring is deterministic spec math — the LLM only writes the verdict prose."""

from __future__ import annotations

from mallory_engine.services.tender_scoring import (
    _fit_level,
    _parse_requirements,
    _score_product,
)


def test_parse_requirements_extracts_slots_and_ops() -> None:
    reqs = _parse_requirements(
        [
            {"label": "Range", "value": "≥ 45 km"},
            {"label": "Weight", "value": "< 18 tonnes"},
            {"label": "System", "value": "155mm / 52-cal"},
        ]
    )
    assert reqs["range_km"] == (">=", 45.0)
    assert reqs["weight_t"] == ("<=", 18.0)
    assert reqs["calibre_mm"][1] == 155.0


def test_atags_is_high_fit_for_45km_under_18t() -> None:
    reqs = _parse_requirements(
        [{"label": "Range", "value": "≥ 45 km"}, {"label": "Weight", "value": "< 18 tonnes"}]
    )
    specs = {"range_km": 48.0, "weight_t": 18.0, "calibre_mm": 155.0}
    pct, lines = _score_product(reqs, specs, "ATAGS")
    assert pct >= 80
    assert all(line[0] == "up" for line in lines)


def test_weak_specs_lower_the_score() -> None:
    reqs = _parse_requirements(
        [{"label": "Range", "value": "≥ 45 km"}, {"label": "Weight", "value": "< 18 tonnes"}]
    )
    specs = {"range_km": 30.0, "weight_t": 30.0}  # short range, too heavy
    pct, lines = _score_product(reqs, specs, "MArG")
    assert pct < 80
    assert any(line[0] == "down" for line in lines)


def test_fit_level_thresholds() -> None:
    assert _fit_level(85) == "high"
    assert _fit_level(60) == "medium"
    assert _fit_level(30) == "low"
