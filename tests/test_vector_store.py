from dataclasses import replace
from unittest.mock import Mock

import pytest
from qdrant_client import QdrantClient

from faq_agent.config import Settings
from faq_agent.schemas import Chunk
from faq_agent.vectordb.vector_store import ChromaVectorStore, QdrantVectorStore


def test_qdrant_version_isolation_and_idempotence():
    s = replace(Settings(), vector_dimensions=2)
    client = QdrantClient(":memory:")
    first = QdrantVectorStore(s, client)
    assert not first.version_exists()
    old = Chunk("a", "source", "old public text", {"title": "Public", "source_path": "PRIVATE"})
    first.upsert([old], [[1.0, 0.0]])
    assert first.version_exists()
    first.upsert([old], [[1.0, 0.0]])
    second = QdrantVectorStore(replace(s, corpus_version="v2"), client)
    second.upsert([Chunk("b", "source", "new public text")], [[1.0, 0.0]])
    results = first.search([1.0, 0.0], 10)
    assert len(results) == 1 and results[0].text == "old public text"
    assert "source_path" not in results[0].metadata
    assert second.search([1.0, 0.0], 10)[0].text == "new public text"
    first.close()


def test_dimension_failure_before_any_write():
    client = Mock()
    store = QdrantVectorStore(replace(Settings(), vector_dimensions=2), client)
    with pytest.raises(ValueError):
        store.upsert([Chunk("a", "s", "text")], [[1.0]])
    client.create_collection.assert_not_called()
    client.upsert.assert_not_called()


def test_chroma_contract_and_filter():
    client, collection = Mock(), Mock()
    client.get_collection.return_value = collection
    collection.query.return_value = {
        "documents": [["text"]],
        "distances": [[0.25]],
        "metadatas": [[{"chunk_id": "a", "corpus_version": "v1"}]],
    }
    s = replace(Settings(), vector_store="chroma", vector_dimensions=2)
    store = ChromaVectorStore(s, client)
    assert store.search([1.0, 0.0], 4)[0].score == 0.75
    assert collection.query.call_args.kwargs["where"] == {"corpus_version": "v1"}
