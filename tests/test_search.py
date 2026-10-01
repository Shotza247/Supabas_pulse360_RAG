import pytest
from fastapi.testclient import TestClient

from faq_agent.api.routes import create_app


def test_search_without_llm():
    hit = {
        "chunk_id": "demo",
        "title": "Sample",
        "section": "Atlas",
        "text": "Atlas uses Python",
        "score": 0.8,
        "vector_score": 0.8,
        "rerank_score": 0.9,
    }
    client = TestClient(create_app(searcher=lambda question: [hit]))
    response = client.post("/search", json={"question": "What is Atlas?"})
    assert response.status_code == 200
    assert response.json()["matches"] == [hit]
    assert response.json()["request_id"]


@pytest.mark.parametrize("body", [{}, {"question": " "}, {"question": 42}, {"question": "a" * 801}])
def test_search_validation(body):
    client = TestClient(create_app(searcher=lambda question: []))
    assert client.post("/search", json=body).status_code == 422


def test_search_redacts_errors():
    def fail(question):
        raise RuntimeError("SECRET token")

    client = TestClient(create_app(searcher=fail))
    response = client.post("/search", json={"question": "What is Atlas?"})
    assert response.status_code == 503
    assert "SECRET" not in response.text
