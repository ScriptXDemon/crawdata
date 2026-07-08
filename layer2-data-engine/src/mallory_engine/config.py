"""Application configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+psycopg://mallory:mallory@localhost:5432/mallory"

    # LLM provider: "stub" (deterministic, no key) | "ollama" | "anthropic" | "openrouter"
    llm_provider: str = "stub"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    # OpenRouter (OpenAI-compatible). Used for Mallory chat, enrichment, and reports.
    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemini-2.5-flash"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Ollama (OpenAI-compatible). Local by default; flip base_url to the remote door
    # (https://ollama.i3softlab.com/v1) + set ollama_api_key when it opens.
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_api_key: str = ""  # blank for local; set for the remote door
    ollama_model_fast: str = "qwen2.5:14b"   # classify / judge / extract
    ollama_model_deep: str = "qwen2.5:14b"   # synthesis / verdicts / reports
    ollama_model_vision: str = ""            # empty ⇒ vision tasks disabled (fallback)
    ollama_model_embed: str = ""             # empty ⇒ embedding retrieval disabled
    llm_timeout_s: int = 120                 # a 14b on GPU is slower than a hosted API
    llm_num_ctx: int = 8192
    llm_cache_enabled: bool = True

    # Reference seed data directory (bundled JSON → ref_* tables)
    seed_dir: str = "./seed_data"

    # In-process scheduler: run the pipeline over pending staging rows automatically.
    # ponytail: asyncio loop; upgrade to APScheduler/job-queue when multiple cadences exist.
    scheduler_enabled: bool = False
    scheduler_interval_s: int = 120

    # Crawler Ingest API base URL (for proxying asset requests).
    # Set to http://<crawler-ip>:8077 when L2 is on a different machine.
    crawler_ingest_url: str = "http://localhost:9090"

    # Patent source. USPTO Open Data Portal (api.uspto.gov) is the real free API — it
    # replaced PatentsView (retired 2025). Get a free key at data.uspto.gov (My ODP →
    # API keys). Blank ⇒ connector falls back to keyless Google Patents (throttled).
    uspto_api_key: str = ""
    uspto_base_url: str = "https://api.uspto.gov"

    # CORS origins for the Layer 3 client (comma-separated)
    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
