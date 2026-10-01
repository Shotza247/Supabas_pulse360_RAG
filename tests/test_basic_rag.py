from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from faq_agent.api import routes as api
from faq_agent.config import Settings
from faq_agent.llm.client import synthesize, validate_answer
from faq_agent.retrieval import retriever
from faq_agent.schemas import RetrievedChunk

HIT = {
    "chunk_id": "a",
    "text": "Refunds within 14 days.",
    "title": "FAQ",
    "section": "Refunds",
    "score": 0.8,
    "vector_score": 0.8,
    "rerank_score": None,
}


def test_search_and_ask_share_search(monkeypatch):
    search = Mock(return_value=[HIT])
    response = Mock()
    response.json.return_value = {
        "choices": [{"message": {"content": '{"answer":"Within 14 days.","citation_ids":["a"]}'}}]
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(api, "search_question", search)
    monkeypatch.setattr(retriever, "search_question", search)
    monkeypatch.setattr(
        retriever, "get_settings", lambda: Settings(hf_token="test", llm_model="test")
    )
    monkeypatch.setattr("faq_agent.llm.client.requests.post", post)
    client = TestClient(api.create_app())
    assert client.post("/search", json={"question": "Refunds?"}).json()["matches"] == [HIT]
    result = client.post("/ask", json={"question": "Refunds?"})
    assert result.status_code == 200
    assert result.json()["status"] == "answered"
    assert result.json()["sources"][0]["vector_score"] == 0.8
    assert result.json()["sources"][0]["rerank_score"] is None
    assert search.call_count == 2
    post.assert_called_once()


def test_vector_search_needs_no_chat_configuration(monkeypatch):
    store, embeddings = Mock(), Mock()
    store.search.return_value = [RetrievedChunk("a", "Refunds", 0.8)]
    monkeypatch.setattr(retriever, "get_settings", lambda: Settings())
    monkeypatch.setattr(retriever, "build_vector_store", lambda s: store)
    monkeypatch.setattr(retriever, "build_embedding_client", lambda s: embeddings)
    hits = retriever.search_question("Refunds?")
    assert hits[0]["vector_score"] == 0.8
    assert hits[0]["rerank_score"] is None
    store.close.assert_called_once()


def test_empty_search_skips_llm(monkeypatch):
    post = Mock()
    monkeypatch.setattr("faq_agent.llm.client.requests.post", post)
    assert synthesize("q", [], Settings())["status"] == "insufficient_evidence"
    post.assert_not_called()


@pytest.mark.parametrize("raw", ["bad json", "{}", '{"answer":"x","citation_ids":["invented"]}'])
def test_invalid_citations_abstain(raw):
    assert validate_answer(raw, [HIT])["status"] == "insufficient_evidence"
