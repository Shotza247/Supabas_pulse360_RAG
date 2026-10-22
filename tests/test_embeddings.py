from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from faq_agent.config import Settings
from faq_agent.embeddings.embedder import HFEmbeddingClient, _MAX_RETRIES, validate_vectors


@pytest.mark.parametrize(
    "vectors", [[], [[1]], [[0, 0]], [[float("nan"), 1]], [[True, 1]], [[float("inf"), 1]], "oops"]
)
def test_rejects_invalid_vectors(vectors):
    with pytest.raises(ValueError):
        validate_vectors(vectors, 1, 2)


def test_hosted_embeddings_prefix_batch_and_shape(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"]["inputs"])
        return Mock(status_code=200, json=lambda: [[1.0, 0.0] for _ in kwargs["json"]["inputs"]])

    monkeypatch.setattr("faq_agent.embeddings.embedder.requests.post", post)
    s = replace(
        Settings(), embedding_url="https://test/embed", hf_token="test", vector_dimensions=2
    )
    client = HFEmbeddingClient(s)
    assert len(client.embed_documents(["abc"] * 17)) == 17
    assert [len(c) for c in calls] == [16, 1]
    client.embed_query("skills")
    assert calls[-1] == [s.embedding_query_prefix + "skills"]


def test_embed_query_rejects_empty_text():
    s = replace(Settings(), embedding_url="https://test/embed", hf_token="test", vector_dimensions=2)
    client = HFEmbeddingClient(s)
    with pytest.raises(ValueError, match="non-empty"):
        client.embed_query("")
    with pytest.raises(ValueError, match="non-empty"):
        client.embed_query("   ")


def test_embed_retries_on_transient_error(monkeypatch):
    """A 429 on the first call should be retried; success on second call is returned."""
    attempt = {"n": 0}

    def post(url, **kwargs):
        attempt["n"] += 1
        if attempt["n"] < 2:
            return Mock(status_code=429)
        return Mock(status_code=200, json=lambda: [[1.0, 0.0]])

    monkeypatch.setattr("faq_agent.embeddings.embedder.requests.post", post)
    monkeypatch.setattr("faq_agent.embeddings.embedder.time.sleep", lambda _: None)
    s = replace(Settings(), embedding_url="https://test/embed", hf_token="test", vector_dimensions=2)
    result = HFEmbeddingClient(s).embed_query("hello")
    assert result == [1.0, 0.0]
    assert attempt["n"] == 2


def test_embed_raises_after_all_retries_exhausted(monkeypatch):
    """Persistent 503 should raise after _MAX_RETRIES attempts."""
    calls = {"n": 0}

    def post(url, **kwargs):
        calls["n"] += 1
        r = Mock(status_code=503)
        r.raise_for_status.side_effect = Exception("503 Service Unavailable")
        return r

    monkeypatch.setattr("faq_agent.embeddings.embedder.requests.post", post)
    monkeypatch.setattr("faq_agent.embeddings.embedder.time.sleep", lambda _: None)
    s = replace(Settings(), embedding_url="https://test/embed", hf_token="test", vector_dimensions=2)
    with pytest.raises(Exception, match="503"):
        HFEmbeddingClient(s).embed_query("hello")
    assert calls["n"] == _MAX_RETRIES
