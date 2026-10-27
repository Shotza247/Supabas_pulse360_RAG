"""Environment configuration; credentials never appear in public responses."""

import os
from dataclasses import dataclass, fields
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    app_env: str = "local"
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5500"
    supabase_database_url: str = ""
    collection: str = "faq_documents"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_revision: str = "main"
    vector_dimensions: int = 384
    hf_token: str = ""
    embedding_url: str = ""
    embedding_query_prefix: str = "Represent this sentence for searching relevant passages: "
    embedding_document_prefix: str = ""
    llm_base_url: str = "https://router.huggingface.co/v1"
    llm_provider: str = "huggingface"
    llm_model: str = ""
    openai_api_key: str = ""
    request_timeout: int = 30
    context_k: int = 4
    chunk_chars: int = 1000
    chunk_overlap: int = 150

    def __post_init__(self):
        # llm_provider is validated at synthesis time (llm/client.py) so that
        # config loading does not hard-crash when the provider is intentionally
        # overridden for embedding-only or retrieval-only deployments (finding N3).
        if not 0 <= self.chunk_overlap < self.chunk_chars:
            raise ValueError("Require 0 <= CHUNK_OVERLAP < CHUNK_CHARS")
        if not 1 <= self.context_k <= 50:
            raise ValueError("Require 1 <= CONTEXT_K <= 50")
        if self.vector_dimensions < 1 or self.request_timeout < 1:
            raise ValueError("Dimensions and timeout must be positive")
        if not self.collection:
            raise ValueError("COLLECTION is required")
        if self.app_env != "local":
            if any(
                not url.startswith("https://")
                for url in [self.embedding_url, self.llm_base_url]
            ):
                raise ValueError("Production services require HTTPS")

    @property
    def origins(self):
        return [x.strip() for x in self.allowed_origins.split(",") if x.strip()]


# Type coercion map: field.type is a string under PEP 563 (from __future__ import annotations)
_COERCE: dict[str, type] = {"int": int, "float": float, "str": str}


@lru_cache
def get_settings():
    load_dotenv(PROJECT_ROOT / ".env")
    values = {}
    for field in fields(Settings):
        raw = os.getenv(field.name.upper())
        if raw is not None:
            values[field.name] = _COERCE.get(field.type, str)(raw)
    return Settings(**values)
