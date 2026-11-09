from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from faq_agent.config import Settings
from faq_agent.embeddings.embedder import (
    _MAX_RETRIES,
    HFEmbeddingClient,
    OpenAIEmbeddingClient,
    build_embedding_client,
    validate_vectors,
)


@pytest.mark.parametrize(
    "vectors", [[], [[1]], [[0, 0]], [[float("nan"), 1]], [[True, 1]], [[float("inf"), 1]], "oops"]
)
def test_rejects_invalid_vectors(vectors):
    with pytest.raises(ValueError):
        validate_vectors(vectors, 1, 2)


def test_hosted_embeddings_prefix_batch_and_shape(monkeypatch):
    calls = []
    auth_headers = []

    def post(url, **kwargs):
        calls.append(kwargs["json"]["inputs"])
        auth_headers.append(kwargs["headers"]["Authorization"])
        return Mock(status_code=200, json=lambda: [[1.0, 0.0] for _ in kwargs["json"]["inputs"]])

    monkeypatch.setattr("faq_agent.embeddings.embedder.requests.post", post)
    s = replace(
        Settings(), embedding_url="https://test/embed", hf_token="test", vector_dimensions=2
    )
    client = HFEmbeddingClient(s)
    assert len(client.embed_documents(["abc"] * 17)) == 17
    assert set(auth_headers) == {"Bearer test"}
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


def _openai_settings(**overrides):
    base = {
        "embedding_provider": "openai",
        "embedding_url": "https://api.openai.com/v1/embeddings",
        "embedding_model": "text-embedding-3-large",
        "openai_api_key": "sk-test",
        "vector_dimensions": 2,
    }
    return replace(Settings(), **{**base, **overrides})


def test_provider_selects_client_and_rejects_unknown():
    assert isinstance(build_embedding_client(replace(Settings())), HFEmbeddingClient)
    assert isinstance(build_embedding_client(_openai_settings()), OpenAIEmbeddingClient)
    with pytest.raises(ValueError, match="Unsupported embedding_provider"):
        build_embedding_client(replace(Settings(), embedding_provider="nope"))


def test_openai_embeddings_payload_order_and_no_prefix(monkeypatch):
    seen = []

    def post(url, **kwargs):
        seen.append((url, kwargs["headers"]["Authorization"], kwargs["json"]))
        n = len(kwargs["json"]["input"])
        # Return out of order to prove results are re-sorted by "index".
        items = [{"index": i, "embedding": [float(i + 1), 0.5]} for i in range(n)]
        return Mock(status_code=200, json=lambda: {"data": list(reversed(items))})

    monkeypatch.setattr("faq_agent.embeddings.embedder.requests.post", post)
    client = OpenAIEmbeddingClient(_openai_settings())
    vectors = client.embed_documents(["a", "b", "c"])
    assert vectors == [[1.0, 0.5], [2.0, 0.5], [3.0, 0.5]]
    url, auth, payload = seen[0]
    assert url == "https://api.openai.com/v1/embeddings"
    assert auth == "Bearer sk-test"
    assert payload == {"model": "text-embedding-3-large", "input": ["a", "b", "c"], "dimensions": 2}
    client.embed_query("skills")
    assert seen[-1][2]["input"] == ["skills"]  # bge query prefix is not applied to OpenAI


def test_openai_embeddings_batches_of_100(monkeypatch):
    sizes = []

    def post(url, **kwargs):
        n = len(kwargs["json"]["input"])
        sizes.append(n)
        return Mock(
            status_code=200,
            json=lambda: {"data": [{"index": i, "embedding": [1.0, 1.0]} for i in range(n)]},
        )

    monkeypatch.setattr("faq_agent.embeddings.embedder.requests.post", post)
    assert len(OpenAIEmbeddingClient(_openai_settings()).embed_documents(["x"] * 101)) == 101
    assert sizes == [100, 1]


def test_openai_embeddings_require_key_and_url_and_valid_text():
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIEmbeddingClient(_openai_settings(openai_api_key="")).embed_query("hi")
    with pytest.raises(ValueError, match="EMBEDDING_URL"):
        OpenAIEmbeddingClient(_openai_settings(embedding_url="")).embed_query("hi")
    with pytest.raises(ValueError, match="non-empty"):
        OpenAIEmbeddingClient(_openai_settings()).embed_query("  ")


def test_openai_embeddings_reject_wrong_dimension(monkeypatch):
    def post(url, **kwargs):
        return Mock(status_code=200, json=lambda: {"data": [{"index": 0, "embedding": [1.0, 2.0, 3.0]}]})

    monkeypatch.setattr("faq_agent.embeddings.embedder.requests.post", post)
    with pytest.raises(ValueError, match="dimension"):
        OpenAIEmbeddingClient(_openai_settings()).embed_query("hi")
