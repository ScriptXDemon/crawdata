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

    # Ollama (OpenAI-compatible). Local by default; flip base_url to the remote farm
    # (https://ollama.i3softlab.com/v1) + set ollama_api_key. Farm models: text-model, vlm-model.
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_api_key: str = ""  # blank for local; the sk-farm-... key for the remote farm
    ollama_model_fast: str = "qwen2.5:7b"      # extract / classify / captions-judge — high volume
    ollama_model_deep: str = "qwen2.5:14b"     # synthesis / verdicts / reports — heavy reasoning
    ollama_model_vision: str = "qwen2.5vl:7b"  # image captions / equipment recognition
    # Embeddings run on their OWN endpoint (the farm has no embed model, so embed stays local
    # even when chat/vision point at the farm). Blank base_url ⇒ reuse ollama_base_url.
    ollama_embed_base_url: str = "http://localhost:11434/v1"
    ollama_embed_api_key: str = ""
    ollama_model_embed: str = "nomic-embed-text"  # semantic dedup / retrieval (Phase C)
    llm_timeout_s: int = 120                 # a 14b on GPU is slower than a hosted API
    llm_num_ctx: int = 8192
    llm_cache_enabled: bool = True

    # Reference seed data directory (bundled JSON → ref_* tables)
    seed_dir: str = "./seed_data"

    # In-process scheduler: run the pipeline over pending staging rows automatically.
    # ponytail: asyncio loop; upgrade to APScheduler/job-queue when multiple cadences exist.
    scheduler_enabled: bool = False
    scheduler_interval_s: int = 120

    # Crawler Ingest API base URL (fallback asset proxy when MinIO isn't configured).
    crawler_ingest_url: str = "http://localhost:9090"

    # MinIO / S3 object store — where the crawler wrote the blobs. When set, L2 reads
    # asset bytes straight from MinIO by the s3://mallory-raw/... URI (no crawler proxy).
    minio_endpoint: str = ""              # e.g. "localhost:9000"; blank ⇒ proxy via crawler
    minio_access_key: str = "mallory"
    minio_secret_key: str = "mallory123"
    minio_bucket: str = "mallory-raw"
    minio_secure: bool = False

    # Patent source, tried in order: SerpApi (engine=google_patents, reliable, needs a key)
    # → USPTO ODP (free key) → keyless Google Patents (throttled). Set whichever you have.
    serpapi_key: str = ""
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
