"""Deterministic output validators — zero cost, run on every structured LLM output.

These are cheap guardrails, not the LLM-judge (that arrives with synthesis in a later phase).
Each returns a list of problem strings (empty = clean). They never raise.
"""

from __future__ import annotations

import re

# number-with-unit tokens: ₹4,500 cr | $320M | 155mm | 52 km | 36 units | 12%
_NUM_UNIT = re.compile(
    r"(?:₹|\$|€|£)?\s?\d[\d,]*(?:\.\d+)?\s?"
    r"(?:%|mm|cm|km|kg|cr|crore|lakh|bn|billion|million|units?|guns?|m\b|k\b|M\b|B\b)",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    return s.replace(",", "").replace(" ", "").lower()


def numbers_grounded(output_text: str, evidence_text: str) -> list[str]:
    """Every number-with-unit in the output must appear in the evidence (comma/space-insensitive)."""
    ev = _norm(evidence_text)
    problems = []
    for m in _NUM_UNIT.findall(output_text):
        if _norm(m) not in ev:
            problems.append(f"ungrounded number: {m.strip()!r}")
    return problems


def length_bounds(value: str, *, field: str, max_len: int) -> list[str]:
    if len(value) > max_len:
        return [f"{field} exceeds {max_len} chars ({len(value)})"]
    return []


def enum_valid(value: str, *, field: str, allowed: set[str]) -> list[str]:
    if value not in allowed:
        return [f"{field}={value!r} not in {sorted(allowed)}"]
    return []
