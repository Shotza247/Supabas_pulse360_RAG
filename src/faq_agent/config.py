"""Environment configuration; credentials never appear in public responses."""

import hashlib
import json
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
    vector_store: str = "supabase"
    vector_url: str = "http://localhost:6333"
    vector_api_key: str = ""
    supabase_database_url: str = ""
    collection: str = "faq_documents"
    corpus_version: str = "v1"
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
    request_timeout: int = 30
    context_k: int = 4
    chunk_chars: int = 1000
    chunk_overlap: int = 150

    def __post_init__(self):
        if self.llm_provider != "huggingface":
            raise ValueError("Only hosted Hugging Face synthesis is currently supported")
        if self.vector_store not in {"supabase", "qdrant", "chroma"}:
            raise ValueError("VECTOR_STORE must be supabase, qdrant or chroma")
        if not 0 <= self.chunk_overlap < self.chunk_chars:
            raise ValueError("Require 0 <= CHUNK_OVERLAP < CHUNK_CHARS")
        if not 1 <= self.context_k <= 50:
            raise ValueError("Require 1 <= CONTEXT_K <= 50")
        if self.vector_dimensions < 1 or self.request_timeout < 1:
            raise ValueError("Dimensions and timeout must be positive")
        if not self.collection or not self.corpus_version:
            raise ValueError("Collection and corpus version are required")
        if self.app_env != "local":
            urls = [self.embedding_url, self.llm_base_url]
            if self.vector_store != "supabase":
                urls.append(self.vector_url)
            if any(not url.startswith("https://") for url in urls):
                raise ValueError("Production services require HTTPS")

    @property
    def origins(self):
        return [x.strip() for x in self.allowed_origins.split(",") if x.strip()]

    @property
    def index_name(self):
        spec = [
            self.embedding_model,
            self.embedding_revision,
            self.vector_dimensions,
            self.embedding_query_prefix,
            self.embedding_document_prefix,
            self.chunk_chars,
            self.chunk_overlap,
            "chunk-v2",
        ]
        digest = hashlib.sha256(json.dumps(spec).encode()).hexdigest()[:12]
        return f"{self.collection}-{digest}"


@lru_cache
def get_settings():
    load_dotenv(PROJECT_ROOT / ".env")
    values = {}
    for field in fields(Settings):
        raw = os.getenv(field.name.upper())
        if raw is not None:
            values[field.name] = field.type(raw) if field.type in (int, float) else raw
    return Settings(**values)
