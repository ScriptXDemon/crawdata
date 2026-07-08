"""Offline eval — grades the deterministic stub against golden properties. Always in CI."""

from __future__ import annotations

import pytest

from mallory_engine.services.llm.stub import StubLLMProvider

from .properties import load_cases, run_case


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: f"{c['task']}:{c.get('note', '')[:30]}")
def test_stub_case(case: dict) -> None:
    fails = run_case(StubLLMProvider(), case)
    assert not fails, f"{case['task']} failed properties: {fails}"
