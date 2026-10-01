from dataclasses import replace
from unittest.mock import Mock

import pytest

from faq_agent.config import Settings
from faq_agent.embeddings.embedder import HFEmbeddingClient, validate_vectors


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
        return Mock(json=lambda: [[1.0, 0.0] for _ in kwargs["json"]["inputs"]])

    monkeypatch.setattr("faq_agent.embeddings.embedder.requests.post", post)
    s = replace(
        Settings(), embedding_url="https://test/embed", hf_token="test", vector_dimensions=2
    )
    client = HFEmbeddingClient(s)
    assert len(client.embed_documents(["abc"] * 17)) == 17
    assert [len(c) for c in calls] == [16, 1]
    client.embed_query("skills")
    assert calls[-1] == [s.embedding_query_prefix + "skills"]
