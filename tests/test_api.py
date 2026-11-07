import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from faq_agent.api.documents import get_library, require_admin
from faq_agent.api.routes import _RATE_LIMIT, _rate_counters, create_app
from faq_agent.config import Settings
from faq_agent.rate_limiter import _current_window


def client(answerer=None):
    return TestClient(
        create_app(
            answerer
            or (
                lambda q: {
                    "answer": "I do not know.",
                    "sources": [],
                    "status": "insufficient_evidence",
                }
            )
        )
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"question": 3},
        {"question": " "},
        {"question": "a" * 801},
        {"question": "projects", "top_k": 999},
    ],
)
def test_request_validation(body):
    assert client().post("/ask", json=body).status_code == 422


def test_invalid_json():
    assert (
        client().post("/ask", content="{", headers={"Content-Type": "application/json"}).status_code
        == 422
    )


def test_success_and_liveness():
    c = client()
    assert c.get("/health").json() == {"status": "ok", "kind": "liveness"}
    response = c.post("/ask", json={"question": "projects"})
    assert response.status_code == 200
    assert response.json()["request_id"]


def test_error_redaction():
    def fail(question):
        raise ValueError("SECRET credential and private path")

    response = client(fail).post("/ask", json={"question": "projects"})
    assert response.status_code == 503
    assert "SECRET" not in response.text


def test_cors():
    response = client().options(
        "/ask",
        headers={"Origin": "https://untrusted.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in response.headers


def test_library_api_key_and_cors(monkeypatch):
    from types import SimpleNamespace

    app = create_app()
    app.dependency_overrides[get_library] = lambda: SimpleNamespace(list_collections=list)
    c = TestClient(app)

    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    assert c.get("/collections").status_code == 401

    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    with pytest.raises(HTTPException) as error:
        require_admin("cl\u00e9")
    assert error.value.status_code == 401
    response = c.get("/collections", headers={"X-API-Key": "test-admin-key"})
    assert response.status_code == 200

    preflight = c.options(
        "/collections",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-api-key",
        },
    )
    assert preflight.status_code == 200
    assert "x-api-key" in preflight.headers["access-control-allow-headers"].lower()


def test_service_errors_documented():
    schema = client().get("/openapi.json").json()
    for path in ("/search", "/ask"):
        response = schema["paths"][path]["post"]["responses"]["503"]
        assert response["content"]["application/json"]["schema"]["$ref"].endswith(
            "/ServiceErrorResponse"
        )


def test_rate_limit_returns_429():
    """After _RATE_LIMIT requests from the same IP in one window, the next returns 429."""
    _rate_counters.clear()
    c = client()
    # Issue _RATE_LIMIT successful requests
    for _ in range(_RATE_LIMIT):
        r = c.post("/ask", json={"question": "hello"})
        assert r.status_code == 200
    # The next one should be rate-limited
    r = c.post("/ask", json={"question": "hello"})
    assert r.status_code == 429


def test_rate_limit_window_is_shared_across_instances(monkeypatch):
    monkeypatch.setattr("faq_agent.rate_limiter.time.time", lambda: 121)
    assert _current_window() == 2


def test_search_hit_has_no_score_field():
    """SearchHit response must not contain a redundant 'score' alias (finding #16)."""
    hit = {
        "chunk_id": "demo",
        "title": "Doc",
        "section": "S",
        "text": "content",
        "vector_score": 0.75,
    }
    c = TestClient(create_app(searcher=lambda q: [hit]))
    matches = c.post("/search", json={"question": "test?"}).json()["matches"]
    assert len(matches) == 1
    assert "score" not in matches[0]
    assert matches[0]["vector_score"] == 0.75


def test_non_huggingface_provider_settings_loads():
    """Settings must not raise when llm_provider != 'huggingface' (finding N3)."""
    s = Settings(llm_provider="openai")
    assert s.llm_provider == "openai"
