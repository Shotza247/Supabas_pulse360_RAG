"""Hosted TEI embeddings; the API does not download model weights."""

import math
from typing import Protocol

import requests


class EmbeddingClient(Protocol):
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


def validate_vectors(vectors, count, dimensions):
    if not isinstance(vectors, list) or len(vectors) != count:
        raise ValueError("Embedding response count mismatch")
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimensions:
            raise ValueError("Embedding response dimension mismatch")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
            raise ValueError("Embedding response contains invalid numbers")
        if not any(vector):
            raise ValueError("Embedding response contains a zero vector")
    return vectors


class HFEmbeddingClient:
    def __init__(self, settings):
        self.settings = settings
        self.dimensions = settings.vector_dimensions

    def _embed(self, texts):
        if not texts:
            return []
        if not self.settings.embedding_url or not self.settings.hf_token:
            raise ValueError("Configure hosted TEI EMBEDDING_URL and HF_TOKEN")
        vectors = []
        for offset in range(0, len(texts), 16):
            batch = texts[offset : offset + 16]
            response = requests.post(
                self.settings.embedding_url,
                headers={"Authorization": f"Bearer {self.settings.hf_token}"},
                json={"inputs": batch, "truncate": False},
                timeout=self.settings.request_timeout,
            )
            response.raise_for_status()
            vectors.extend(validate_vectors(response.json(), len(batch), self.dimensions))
        return vectors

    def embed_documents(self, texts):
        return self._embed([self.settings.embedding_document_prefix + t for t in texts])

    def embed_query(self, text):
        return self._embed([self.settings.embedding_query_prefix + text])[0]


def build_embedding_client(settings):
    return HFEmbeddingClient(settings)
