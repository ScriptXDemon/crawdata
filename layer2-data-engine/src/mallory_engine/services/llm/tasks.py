"""Task layer — every domain method written once, provider-agnostic.

Flow for each structured call (``_run_structured``):
  1. cache lookup on (task, input_hash, model) → reuse a prior 'ok' output
  2. transport.complete(json_schema=...) → raw text
  3. parse (brace-extract) + Pydantic validate; one retry with the error appended
  4. deterministic validators (numbers grounded, length, enum)
  5. write the llm_runs ledger row
  6. any failure → deterministic fallback (never raises into the pipeline)

Numbers and rankings are never model-computed; this layer owns prose and judgment only.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from ...config import Settings
from . import cache, schemas, validators
from .stub import ANCHOR, StubLLMProvider
from .transport import ChatRequest, ChatTransport


def norm_cite(c: object) -> str:
    """Normalize a model-emitted evidence id: '[sig:1]' / ' SIG:1 ' → 'sig:1'."""
    return str(c).strip().strip("[]()").strip().lower()


def _extract_json(raw: str) -> dict | None:
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:  # tolerate prose around the object
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
    except Exception:
        pass
    return None


class OllamaTasksProvider:
    """LLMProvider implementation backed by a transport, with per-task deterministic fallback.

    ``db`` is optional: pass a session to enable caching + the ledger; omit it (tests, CI) and
    the provider still works, just without persistence.
    """

    def __init__(self, transport: ChatTransport, settings: Settings, db: Session | None = None) -> None:
        self._t = transport
        self._s = settings
        self._db = db
        self._fallback = StubLLMProvider()
        self._provider_name = settings.llm_provider

    def with_db(self, db: Session) -> "OllamaTasksProvider":
        return OllamaTasksProvider(self._t, self._s, db)

    # ── core ──────────────────────────────────────────────────────────────────
    def _run_structured(
        self, *, task: str, model: str, schema_model: type[BaseModel], json_schema: dict,
        system: str, user: str, evidence_text: str, extra_validate: Callable[[dict], list[str]],
        fallback: Callable[[], dict],
    ) -> dict:
        payload = {"system": system, "user": user}
        ihash = cache.input_hash(task, model, payload)

        cached = cache.lookup(self._db, task, model, ihash)
        if cached is not None:
            return cached

        req = ChatRequest(
            system=system, user=user, model=model, json_schema=json_schema, json_mode=True,
            max_tokens=1024, num_ctx=self._s.llm_num_ctx,
        )
        t0 = time.perf_counter()
        raw = self._t.complete(req)

        parsed: dict | None = None
        problems: list[str] = []
        if raw is not None:
            data = _extract_json(raw)
            if data is not None:
                try:
                    obj = schema_model.model_validate(data)
                    parsed = obj.model_dump()
                except ValidationError as exc:
                    # one retry with the validation error fed back in
                    retry = self._t.complete(ChatRequest(
                        system=system, user=user + f"\n\nYour last reply was invalid: {exc}. "
                        "Return corrected JSON only.", model=model, json_schema=json_schema,
                        json_mode=True, max_tokens=1024, num_ctx=self._s.llm_num_ctx,
                    ))
                    data2 = _extract_json(retry) if retry else None
                    if data2 is not None:
                        try:
                            parsed = schema_model.model_validate(data2).model_dump()
                        except ValidationError:
                            parsed = None

        latency_ms = int((time.perf_counter() - t0) * 1000)

        if parsed is None:
            cache.record(self._db, task=task, model=model, provider=self._provider_name,
                         ihash=ihash, evidence_ids=None, output=None, validator_results=None,
                         status="error" if raw is not None else "fallback", latency_ms=latency_ms)
            return fallback()

        # deterministic validators over the whole rendered output
        out_text = " ".join(str(v) for v in _flatten(parsed))
        problems = validators.numbers_grounded(out_text, evidence_text) + extra_validate(parsed)
        status = "ok" if not problems else "invalid"
        cache.record(self._db, task=task, model=model, provider=self._provider_name, ihash=ihash,
                     evidence_ids=None, output=parsed, validator_results={"problems": problems},
                     status=status, latency_ms=latency_ms)
        if problems:
            return fallback()
        return parsed

    # ── LLMProvider methods ─────────────────────────────────────────────────────
    def classify_signal(self, *, stream: str, event_summary: str, threat_level: str | None) -> dict:
        # dir + control tags are pipeline vocabulary → keep them deterministic (rules the
        # stub already gets right). The LLM owns only the human-facing `lens` label; a free
        # model reliably picks a good lens but won't emit control enums, so we don't ask it to.
        base = self._fallback.classify_signal(
            stream=stream, event_summary=event_summary, threat_level=threat_level)
        lens = self._lens(stream=stream, event_summary=event_summary, dir=base["dir"])
        if lens:
            base["lens"] = lens
        return base

    def _lens(self, *, stream: str, event_summary: str, dir: str) -> str | None:
        out = self._run_structured(
            task="signal_lens", model=self._s.ollama_model_fast,
            schema_model=schemas.LensOut, json_schema=schemas.LENS_SCHEMA,
            system=(f"You are {ANCHOR}'s competitive analyst. Give ONE short analytical lens label "
                    f"(2-4 words, uppercase) for this {stream} signal, e.g. BENCHMARK, "
                    'MARKET / DEMAND, TECH MIGRATION, POLICY / OFFSET. Return JSON {"lens":"..."}.'),
            user=event_summary, evidence_text=event_summary,
            extra_validate=lambda d: [] if d.get("lens") else ["empty lens"],
            fallback=lambda: {"lens": ""},
        )
        return (out.get("lens") or "").strip()[:40] or None

    def enrich_signal(self, *, stream: str, event_summary: str, company: str | None,
                      dir: str, facts: list[list[str]]) -> dict:
        fact_text = "; ".join(f"{k}: {v}" for k, v in facts)
        return self._run_structured(
            task="enrich_signal", model=self._s.ollama_model_deep,
            schema_model=schemas.EnrichOut, json_schema=schemas.ENRICH_SCHEMA,
            system=(f"You are {ANCHOR}'s analyst. Explain what this {stream} signal (direction: {dir}) "
                    f"means for {ANCHOR}. Use ONLY the facts given; do not invent numbers. Return JSON "
                    '{"sowhat":"<=300 chars","what_text","why_text",'
                    '"lens_reads":[["LENS","read"]],"actions":[["label","text"]],"suggest":["..."]}.'),
            user=f"Signal: {event_summary}\nFacts: {fact_text}",
            evidence_text=event_summary + " " + fact_text,
            extra_validate=lambda d: validators.length_bounds(
                d.get("sowhat", ""), field="sowhat", max_len=400),
            fallback=lambda: self._fallback.enrich_signal(
                stream=stream, event_summary=event_summary, company=company, dir=dir, facts=facts),
        )

    def tender_verdict(self, *, title: str, best_fit_pct: int, match_summary: str) -> dict:
        return self._run_structured(
            task="tender_verdict", model=self._s.ollama_model_deep,
            schema_model=schemas.TenderVerdictOut, json_schema=schemas.TENDER_VERDICT_SCHEMA,
            system=(f"You are {ANCHOR}'s bid strategist. Give a go/maybe/pass verdict for {ANCHOR}. "
                    f"The fit score of {best_fit_pct}% is computed — cite it, don't change it. "
                    'Return JSON {"lean":"go|maybe|pass","lean_text":"..."}.'),
            user=f"Tender: {title}. Best {ANCHOR} fit {best_fit_pct}%. {match_summary}",
            evidence_text=f"{best_fit_pct}% {match_summary}",
            extra_validate=lambda d: validators.enum_valid(
                d.get("lean", ""), field="lean", allowed={"go", "maybe", "pass"}),
            fallback=lambda: self._fallback.tender_verdict(
                title=title, best_fit_pct=best_fit_pct, match_summary=match_summary),
        )

    def chat(self, *, system: str, context: str, message: str) -> str:
        req = ChatRequest(
            system=system, user=f"Context:\n{context}\n\nQuestion: {message}",
            model=self._s.ollama_model_deep, max_tokens=1024, num_ctx=self._s.llm_num_ctx,
        )
        out = self._t.complete(req)
        return out or self._fallback.chat(system=system, context=context, message=message)

    # ── Synthesis engines (S-22/23/24). Empty dict = generation failed; the calling
    # service falls back to its deterministic template (fail-safe: never publish bad output).

    def _cites_valid(self, pack_ids: set[str]) -> Callable[[dict], list[str]]:
        def check(d: dict) -> list[str]:
            problems: list[str] = []
            def walk(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k == "cites" and isinstance(v, list):
                            # normalize in place: models emit "[sig:1]" / " sig:1 " variants
                            v[:] = [norm_cite(c) for c in v]
                            problems.extend(
                                f"unknown cite {c!r}" for c in v if c not in pack_ids)
                        else:
                            walk(v)
                elif isinstance(obj, list):
                    for v in obj:
                        walk(v)
            walk(d)
            return problems
        return check

    def synthesize_competitor(self, *, competitor: str, anchor_frame: str, pack_text: str,
                              pack_ids: set[str], exemplar: str) -> dict:
        return self._run_structured(
            task="synthesize_competitor", model=self._s.ollama_model_deep,
            schema_model=schemas.SynthesisOut, json_schema=schemas.SYNTHESIS_SCHEMA,
            system=(f"You are {ANCHOR}'s senior competitive analyst. Synthesize a strategic read "
                    f"of the competitor {competitor} vs {ANCHOR}, using ONLY the evidence items "
                    "below — never outside knowledge. Each vulnerability MUST list the evidence "
                    "ids supporting it in its `cites`; uncited vulnerabilities are DISCARDED. "
                    "If evidence is insufficient for a section, write what's missing into `gaps` "
                    "instead of inventing. The exemplar below (a DIFFERENT competitor) shows the "
                    "analytical voice only — never reuse its facts:\n"
                    f"{exemplar}\n"
                    f"{ANCHOR} frame: {anchor_frame}"),
            user=f"Evidence for {competitor}:\n{pack_text}",
            evidence_text=pack_text,
            extra_validate=self._cites_valid(pack_ids),
            fallback=dict,
        )

    def matchup_verdict(self, *, kssl_name: str, comp_name: str, edge_score: int,
                        spec_text: str, pack_ids: set[str]) -> dict:
        return self._run_structured(
            task="matchup_verdict", model=self._s.ollama_model_deep,
            schema_model=schemas.MatchupVerdictOut, json_schema=schemas.MATCHUP_VERDICT_SCHEMA,
            system=(f"You are {ANCHOR}'s analyst. Write a 1-2 sentence head-to-head verdict for "
                    f"{kssl_name} ({ANCHOR}) vs {comp_name}. The computed edge score is "
                    f"{edge_score}/100 ({ANCHOR}'s edge) — interpret it, don't change it. "
                    "Use only the spec rows given; no invented numbers. "
                    'Return JSON {"verdict":"..."}.'),
            user=spec_text,
            evidence_text=f"{edge_score} {spec_text}",
            extra_validate=lambda d: [] if d.get("verdict") else ["empty verdict"],
            fallback=dict,
        )

    def field_patterns(self, *, aggregates_text: str, synth_text: str,
                       pack_ids: set[str]) -> dict:
        return self._run_structured(
            task="field_patterns", model=self._s.ollama_model_deep,
            schema_model=schemas.FieldPatternsOut, json_schema=schemas.FIELD_PATTERNS_SCHEMA,
            system=(f"You are {ANCHOR}'s senior analyst. From the deterministic aggregates and "
                    "competitor syntheses below, name 3-6 cross-field patterns (what keeps "
                    "happening across the whole competitive field). Cite aggregate ids. "
                    "Each pattern: title, summary, exceptions, bottom_line for KSSL."),
            user=f"Aggregates:\n{aggregates_text}\n\nSyntheses:\n{synth_text}",
            evidence_text=aggregates_text + " " + synth_text,
            extra_validate=self._cites_valid(pack_ids),
            fallback=dict,
        )


def _flatten(obj: object) -> list:
    """Yield all leaf string/number values from a nested dict/list (for validation scanning)."""
    out: list = []
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(_flatten(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_flatten(v))
    else:
        out.append(obj)
    return out
