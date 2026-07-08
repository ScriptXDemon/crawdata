"""LLM provider package.

Public API is unchanged from the old ``services/llm.py`` single module — ``LLMProvider`` and
``get_llm`` are re-exported here, so existing imports (`from ..services.llm import get_llm`)
keep working. Internals are split: transport (HTTP), tasks (prompts + validation + fallback),
schemas, validators, cache/ledger.

Providers by ``settings.llm_provider``:
  stub       → StubLLMProvider (deterministic, offline, default)
  ollama     → OllamaTasksProvider over a local/remote Ollama (OpenAI-compatible)
  openrouter → OllamaTasksProvider over OpenRouter (or legacy provider — both work)
  anthropic  → AnthropicLLMProvider (legacy)
"""

from __future__ import annotations

from typing import Protocol

from ...config import Settings, get_settings
from .providers_legacy import AnthropicLLMProvider, OpenRouterProvider
from .stub import ANCHOR, StubLLMProvider
from .tasks import OllamaTasksProvider
from .transport import build_transport


class LLMProvider(Protocol):
    def classify_signal(self, *, stream: str, event_summary: str, threat_level: str | None) -> dict:
        """Return {dir, lens, tags}."""

    def enrich_signal(self, *, stream: str, event_summary: str, company: str | None,
                      dir: str, facts: list[list[str]]) -> dict:
        """Return {sowhat, what_text, why_text, lens_reads, actions, suggest}."""

    def tender_verdict(self, *, title: str, best_fit_pct: int, match_summary: str) -> dict:
        """Return {lean, lean_text}."""

    def chat(self, *, system: str, context: str, message: str) -> str:
        """Answer a Mallory question grounded in the provided serving context."""


def get_llm(settings: Settings | None = None, *, db=None) -> LLMProvider:
    """Resolve the configured provider. Pass ``db`` to enable the cache + llm_runs ledger."""
    settings = settings or get_settings()
    provider = settings.llm_provider

    if provider == "ollama":
        return OllamaTasksProvider(build_transport(settings), settings, db)
    if provider == "openrouter" and settings.openrouter_api_key:
        # Route through the new task layer (structured output + ledger) when a db is present;
        # fall back to the legacy provider otherwise for exact behavioural parity.
        if db is not None:
            return OllamaTasksProvider(build_transport(settings), settings, db)
        return OpenRouterProvider(settings)
    if provider == "anthropic" and settings.anthropic_api_key:
        return AnthropicLLMProvider(settings)
    return StubLLMProvider()


__all__ = ["LLMProvider", "get_llm", "StubLLMProvider", "ANCHOR"]
