"""Hosted TEI embeddings; the API does not download model weights."""

import math
import time
import logging
from typing import Protocol

import requests

logger = logging.getLogger("faq_agent")

# Retry configuration for transient HuggingFace API errors (429 / 5xx).
# Three attempts: immediate → 1 s → 2 s.
_MAX_RETRIES = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}


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


def _post_with_retry(url: str, headers: dict, payload: dict, timeout: int) -> requests.Response:
    """POST with exponential back-off on 429 / 5xx responses.

    Attempts: 1 (immediate) → 2 (1 s delay) → 3 (2 s delay).
    Any non-retryable status is raised immediately via raise_for_status().
    """
    delay = 1.0
    for attempt in range(1, _MAX_RETRIES + 1):
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if response.status_code not in _RETRY_STATUSES:
            response.raise_for_status()
            return response
        if attempt == _MAX_RETRIES:
            # Final attempt exhausted — raise so the caller gets a clear error.
            logger.error(
                "embed_request_failed url=%s status=%d attempt=%d/%d",
                url,
                response.status_code,
                attempt,
                _MAX_RETRIES,
            )
            response.raise_for_status()
        logger.warning(
            "embed_request_retrying url=%s status=%d attempt=%d/%d delay=%.1fs",
            url,
            response.status_code,
            attempt,
            _MAX_RETRIES,
            delay,
        )
        time.sleep(delay)
        delay *= 2
    # Unreachable, but satisfies type checkers.
    raise RuntimeError("Retry loop exited without returning")  # pragma: no cover


class HFEmbeddingClient:
    def __init__(self, settings):
        self.settings = settings
        self.dimensions = settings.vector_dimensions

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.settings.embedding_url or not self.settings.hf_token:
            raise ValueError("Configure hosted TEI EMBEDDING_URL and HF_TOKEN")
        vectors: list[list[float]] = []
        for offset in range(0, len(texts), 16):
            batch = texts[offset : offset + 16]
            response = _post_with_retry(
                self.settings.embedding_url,
                headers={"Authorization": f"Bearer {self.settings.hf_token}"},
                payload={"inputs": batch, "truncate": False},
                timeout=self.settings.request_timeout,
            )
            vectors.extend(validate_vectors(response.json(), len(batch), self.dimensions))
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([self.settings.embedding_document_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        # H5: guard against empty text — _embed([prefix + ""]) would call the
        # API with an empty string and then index [0] on an empty list if the
        # endpoint rejects it, producing an unhelpful IndexError.
        if not text or not text.strip():
            raise ValueError("Query text must be non-empty")
        return self._embed([self.settings.embedding_query_prefix + text])[0]


def build_embedding_client(settings) -> HFEmbeddingClient:
    return HFEmbeddingClient(settings)
