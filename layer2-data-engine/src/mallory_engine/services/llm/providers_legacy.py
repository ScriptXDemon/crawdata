"""Anthropic and OpenRouter providers — preserved unchanged for back-compat.

These predate the transport/task split (Phase 0). They keep working exactly as before so
``LLM_PROVIDER=anthropic|openrouter`` is not a regression. New capability goes through
``tasks.OllamaTasksProvider``; these are kept as-is.
"""

from __future__ import annotations

import json

from ...config import Settings
from .stub import ANCHOR, StubLLMProvider


class AnthropicLLMProvider:
    """Real enrichment via the Anthropic Messages API (JSON-only responses)."""

    def __init__(self, settings: Settings) -> None:
        import anthropic  # imported lazily so the stub path needs no dependency

        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model
        self._fallback = StubLLMProvider()

    def _ask_json(self, system: str, user: str) -> dict | None:
        try:
            msg = self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=system + " Respond with a single JSON object and nothing else.",
                messages=[{"role": "user", "content": user}],
            )
            return json.loads(msg.content[0].text)
        except Exception:
            return None

    def classify_signal(self, *, stream: str, event_summary: str, threat_level: str | None) -> dict:
        out = self._ask_json(
            f"You are {ANCHOR}'s competitive analyst. Classify a {stream} signal vs {ANCHOR}. "
            'Return {"dir":"threat|watch|fav","lens":"...","tags":["..."]}.',
            event_summary,
        )
        return out or self._fallback.classify_signal(
            stream=stream, event_summary=event_summary, threat_level=threat_level
        )

    def enrich_signal(self, *, stream: str, event_summary: str, company: str | None,
                      dir: str, facts: list[list[str]]) -> dict:
        out = self._ask_json(
            f"You are {ANCHOR}'s analyst. Explain what this {stream} signal means for {ANCHOR}. "
            'Return {"sowhat","what_text","why_text","lens_reads":[["lens","read"]],'
            '"actions":[["label","text"]],"suggest":["..."]}.',
            event_summary,
        )
        return out or self._fallback.enrich_signal(
            stream=stream, event_summary=event_summary, company=company, dir=dir, facts=facts
        )

    def tender_verdict(self, *, title: str, best_fit_pct: int, match_summary: str) -> dict:
        out = self._ask_json(
            f"You are {ANCHOR}'s bid strategist. Give a go/maybe/pass verdict for {ANCHOR}. "
            'Return {"lean":"go|maybe|pass","lean_text":"..."}.',
            f"Tender: {title}. Best fit {best_fit_pct}%. {match_summary}",
        )
        return out or self._fallback.tender_verdict(
            title=title, best_fit_pct=best_fit_pct, match_summary=match_summary
        )

    def chat(self, *, system: str, context: str, message: str) -> str:
        try:
            msg = self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {message}"}],
            )
            return msg.content[0].text
        except Exception:
            return self._fallback.chat(system=system, context=context, message=message)


class OpenRouterProvider:
    """Calls any OpenRouter model via the OpenAI-compatible chat/completions endpoint."""

    def __init__(self, settings: Settings) -> None:
        import httpx

        self._httpx = httpx
        self._key = settings.openrouter_api_key
        self._model = settings.openrouter_model
        self._url = f"{settings.openrouter_base_url}/chat/completions"
        self._fallback = StubLLMProvider()

    def _complete(self, system: str, user: str, *, json_only: bool) -> str | None:
        sys = system + (" Respond with a single JSON object and nothing else." if json_only else "")
        try:
            resp = self._httpx.post(
                self._url,
                headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": sys},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=40,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception:
            return None

    def _json(self, system: str, user: str) -> dict | None:
        raw = self._complete(system, user, json_only=True)
        if not raw:
            return None
        try:
            start, end = raw.find("{"), raw.rfind("}")
            return json.loads(raw[start : end + 1])
        except Exception:
            return None

    def classify_signal(self, *, stream: str, event_summary: str, threat_level: str | None) -> dict:
        out = self._json(
            f"You are {ANCHOR}'s competitive analyst. Classify a {stream} signal vs {ANCHOR}. "
            'Return {"dir":"threat|watch|fav","lens":"...","tags":["..."]}.',
            event_summary,
        )
        return out or self._fallback.classify_signal(
            stream=stream, event_summary=event_summary, threat_level=threat_level
        )

    def enrich_signal(self, *, stream: str, event_summary: str, company: str | None,
                      dir: str, facts: list[list[str]]) -> dict:
        out = self._json(
            f"You are {ANCHOR}'s analyst. Explain what this {stream} signal means for {ANCHOR}. "
            'Return {"sowhat","what_text","why_text","lens_reads":[["lens","read"]],'
            '"actions":[["label","text"]],"suggest":["..."]}.',
            event_summary,
        )
        return out or self._fallback.enrich_signal(
            stream=stream, event_summary=event_summary, company=company, dir=dir, facts=facts
        )

    def tender_verdict(self, *, title: str, best_fit_pct: int, match_summary: str) -> dict:
        out = self._json(
            f"You are {ANCHOR}'s bid strategist. Give a go/maybe/pass verdict for {ANCHOR}. "
            'Return {"lean":"go|maybe|pass","lean_text":"..."}.',
            f"Tender: {title}. Best fit {best_fit_pct}%. {match_summary}",
        )
        return out or self._fallback.tender_verdict(
            title=title, best_fit_pct=best_fit_pct, match_summary=match_summary
        )

    def chat(self, *, system: str, context: str, message: str) -> str:
        out = self._complete(system, f"Context:\n{context}\n\nQuestion: {message}", json_only=False)
        return out or self._fallback.chat(system=system, context=context, message=message)
