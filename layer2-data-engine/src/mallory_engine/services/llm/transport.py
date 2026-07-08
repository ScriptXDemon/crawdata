"""Transport layer — the only per-provider code.

A transport turns a provider-agnostic ``ChatRequest`` into raw text (or None on any failure).
Task logic (prompts, schemas, validation, fallback) lives in ``tasks.py`` and never touches
HTTP. One ``OpenAICompatTransport`` serves the local Ollama server, the remote door, and
OpenRouter — all speak OpenAI ``/v1/chat/completions``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ...config import Settings


@dataclass
class ChatRequest:
    system: str
    user: str
    model: str
    json_mode: bool = False          # ask for a single JSON object back
    json_schema: dict | None = None  # optional structured-output schema (OpenAI response_format)
    images: list[str] = field(default_factory=list)  # data: URIs, for vision-capable models
    max_tokens: int = 1024
    temperature: float = 0.2
    num_ctx: int = 8192


class ChatTransport(Protocol):
    def complete(self, req: ChatRequest) -> str | None: ...
    def embed(self, texts: list[str], *, model: str) -> list[list[float]] | None: ...


class NullTransport:
    """Every call returns None ⇒ tasks fall through to their deterministic fallbacks.

    This is the ``LLM_PROVIDER=stub`` transport: no network, no dependency, no surprises.
    """

    def complete(self, req: ChatRequest) -> str | None:
        return None

    def embed(self, texts: list[str], *, model: str) -> list[list[float]] | None:
        return None


class OpenAICompatTransport:
    """OpenAI-compatible chat/embeddings — serves local Ollama, the remote door, and OpenRouter.

    Structured output: tries ``response_format`` (schema or json_object); Ollama honours
    ``{"type":"json_object"}`` and, on recent builds, json_schema. On any HTTP/parse failure the
    method returns None and the caller falls back — nothing raises into the pipeline.
    """

    def __init__(self, *, base_url: str, api_key: str, timeout_s: int) -> None:
        import httpx  # already a project dependency

        self._httpx = httpx
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout_s

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._key:
            h["Authorization"] = f"Bearer {self._key}"
        return h

    def complete(self, req: ChatRequest) -> str | None:
        user_content: object = req.user
        if req.images:
            # OpenAI multimodal content-parts shape (used when a vision model is configured).
            user_content = [{"type": "text", "text": req.user}] + [
                {"type": "image_url", "image_url": {"url": uri}} for uri in req.images
            ]
        body: dict = {
            "model": req.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": user_content},
            ],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            # Ollama-specific knob; ignored by servers that don't understand it.
            "options": {"num_ctx": req.num_ctx},
        }
        if req.json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "out", "schema": req.json_schema, "strict": False},
            }
        elif req.json_mode:
            body["response_format"] = {"type": "json_object"}

        try:
            resp = self._httpx.post(
                f"{self._base}/chat/completions",
                headers=self._headers(),
                json=body,
                timeout=self._timeout,
            )
            if resp.status_code == 400 and (req.json_schema or req.json_mode):
                # Server rejected structured output — retry in plain mode; task-side
                # brace-extraction recovers the JSON.
                body.pop("response_format", None)
                body["messages"][0]["content"] += (
                    " Respond with a single JSON object and nothing else."
                )
                resp = self._httpx.post(
                    f"{self._base}/chat/completions",
                    headers=self._headers(),
                    json=body,
                    timeout=self._timeout,
                )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception:
            return None

    def embed(self, texts: list[str], *, model: str) -> list[list[float]] | None:
        if not model:
            return None
        try:
            resp = self._httpx.post(
                f"{self._base}/embeddings",
                headers=self._headers(),
                json={"model": model, "input": texts},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return [row["embedding"] for row in resp.json()["data"]]
        except Exception:
            return None


def build_transport(settings: Settings) -> ChatTransport:
    """Pick a transport from settings. Unknown/stub → NullTransport (offline)."""
    provider = settings.llm_provider
    if provider == "ollama":
        return OpenAICompatTransport(
            base_url=settings.ollama_base_url,
            api_key=settings.ollama_api_key,
            timeout_s=settings.llm_timeout_s,
        )
    if provider == "openrouter" and settings.openrouter_api_key:
        return OpenAICompatTransport(
            base_url=settings.openrouter_base_url,
            api_key=settings.openrouter_api_key,
            timeout_s=settings.llm_timeout_s,
        )
    return NullTransport()
