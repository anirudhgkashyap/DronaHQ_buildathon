"""Central configuration. Every tunable comes from the environment so nothing
secret is ever committed. See .env.example for the full list."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- Core -------------------------------------------------------------
    APP_NAME: str = "Atlas SDR"
    ENV: str = "development"
    # sqlite+aiosqlite:///./atlas.db  or  postgresql+asyncpg://user:pass@host/db
    DATABASE_URL: str = "sqlite+aiosqlite:///./atlas.db"
    CORS_ORIGINS: str = "*"

    # ---- DronaHQ agent platform ------------------------------------------
    # Each published DronaHQ agent exposes a REST endpoint. Leave blank to run
    # the whole pipeline against the deterministic simulator instead, which is
    # what makes the demo reproducible without live credentials.
    DRONAHQ_BASE_URL: str = ""
    DRONAHQ_API_KEY: str = ""
    DRONAHQ_AGENT_PROSPECT_GENERATION: str = ""
    DRONAHQ_AGENT_RESEARCH_ENRICHMENT: str = ""
    DRONAHQ_AGENT_ICP_FIT: str = ""
    DRONAHQ_AGENT_OUTREACH_STRATEGY: str = ""
    DRONAHQ_AGENT_PERSONALISATION: str = ""
    DRONAHQ_AGENT_CONVERSATION: str = ""
    DRONAHQ_TIMEOUT_SECONDS: float = 90.0

    # ---- Embeddings / RAG -------------------------------------------------
    # If OPENAI_API_KEY is absent the RAG layer falls back to a deterministic
    # local embedder so retrieval still works offline (see rag/embedder.py).
    OPENAI_API_KEY: str = ""
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIM: int = 1536

    # ---- Outbound channels ------------------------------------------------
    # DRY_RUN keeps every external send inside the database: the message is
    # recorded exactly as it would be sent but no provider call is made.
    CHANNELS_DRY_RUN: bool = True
    SENDGRID_API_KEY: str = ""
    OUTREACH_FROM_EMAIL: str = "sdr@atlas.example"
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""

    # ---- Orchestrator -----------------------------------------------------
    SCHEDULER_ENABLED: bool = True
    SCHEDULER_TICK_SECONDS: int = 20
    MAX_PROSPECTS_PER_TICK: int = 6
    DEFAULT_DAILY_SEND_LIMIT: int = 50

    @property
    def cors_origins(self) -> List[str]:
        raw = (self.CORS_ORIGINS or "*").strip()
        if raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def is_postgres(self) -> bool:
        return self.DATABASE_URL.startswith("postgresql")

    @property
    def dronahq_enabled(self) -> bool:
        return bool(self.DRONAHQ_BASE_URL and self.DRONAHQ_API_KEY)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
