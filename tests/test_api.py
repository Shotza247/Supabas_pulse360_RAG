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


def _query_client():
    ok = {"answer": "ok", "sources": [], "status": "answered"}
    return TestClient(create_app(answerer=lambda q, *a: ok, searcher=lambda q, *a: []))


def test_query_routes_open_in_local_without_key(monkeypatch):
    monkeypatch.delenv("QUERY_API_KEY", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    assert _query_client().post("/ask", json={"question": "hello"}).status_code == 200


def test_query_routes_fail_closed_outside_local(monkeypatch):
    monkeypatch.delenv("QUERY_API_KEY", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    # Settings rejects non-HTTPS service URLs outside local, and EMBEDDING_URL defaults
    # to "". Set both so the test does not depend on what a developer's .env contains.
    monkeypatch.setenv("EMBEDDING_URL", "https://embeddings.example.test")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.test/v1")
    c = _query_client()
    assert c.post("/ask", json={"question": "hello"}).status_code == 401
    assert c.post("/search", json={"question": "hello"}).status_code == 401


def test_query_key_required_and_checked(monkeypatch):
    monkeypatch.setenv("QUERY_API_KEY", "query-key")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-key")
    c = _query_client()
    body = {"question": "hello"}
    assert c.post("/ask", json=body).status_code == 401
    assert c.post("/ask", json=body, headers={"X-API-Key": "wrong"}).status_code == 401
    # The admin key must not unlock query routes.
    assert c.post("/ask", json=body, headers={"X-API-Key": "admin-key"}).status_code == 401
    assert c.post("/ask", json=body, headers={"X-API-Key": "query-key"}).status_code == 200
    assert c.post("/search", json=body, headers={"X-API-Key": "query-key"}).status_code == 200


def test_query_collection_allowlist(monkeypatch):
    monkeypatch.setenv("QUERY_API_KEY", "query-key")
    monkeypatch.setenv("PUBLIC_COLLECTIONS", "pulse360_faq, other_public")
    headers = {"X-API-Key": "query-key"}
    c = _query_client()

    def ask(collection):
        body = {"question": "hello", "collection_id": collection}
        return c.post("/ask", json=body, headers=headers)

    assert ask("pulse360_faq").status_code == 200
    assert ask("other_public").status_code == 200
    denied = ask("private_hr_docs")
    assert denied.status_code == 403
    assert denied.json()["detail"]["message"] == "Collection is not available."


def test_query_collection_defaults_to_configured_collection(monkeypatch):
    monkeypatch.setenv("QUERY_API_KEY", "query-key")
    monkeypatch.delenv("PUBLIC_COLLECTIONS", raising=False)
    monkeypatch.setattr(
        "faq_agent.api.routes.get_settings", lambda: Settings(collection="faq_documents")
    )
    headers = {"X-API-Key": "query-key"}
    c = _query_client()
    ok_body = {"question": "hello", "collection_id": "faq_documents"}
    assert c.post("/ask", json=ok_body, headers=headers).status_code == 200
    bad_body = {"question": "hello", "collection_id": "pulse360_faq"}
    assert c.post("/ask", json=bad_body, headers=headers).status_code == 403


def _ask_from(c, ip, key="query-key", forwarded=True):
    headers = {"X-API-Key": key} if key else {}
    if forwarded and ip:
        headers["X-Forwarded-Client-IP"] = ip
    return c.post("/ask", json={"question": "hello"}, headers=headers)


def test_rate_limit_is_per_forwarded_visitor_when_trusted(monkeypatch):
    monkeypatch.setenv("QUERY_API_KEY", "query-key")
    c = _query_client()
    for _ in range(_RATE_LIMIT):
        assert _ask_from(c, "203.0.113.10").status_code == 200
    # Visitor A is now limited, but visitor B (same Render server) is not.
    assert _ask_from(c, "203.0.113.10").status_code == 429
    assert _ask_from(c, "203.0.113.11").status_code == 200


def test_forwarded_ip_ignored_without_valid_key(monkeypatch):
    monkeypatch.delenv("QUERY_API_KEY", raising=False)
    monkeypatch.setenv("APP_ENV", "local")
    c = _query_client()
    # Open local mode is untrusted: rotating the forwarded header must not dodge the limit.
    for i in range(_RATE_LIMIT):
        assert _ask_from(c, f"203.0.113.{i + 1}", key=None).status_code == 200
    assert _ask_from(c, "203.0.113.200", key=None).status_code == 429


def test_invalid_forwarded_ip_falls_back_to_peer(monkeypatch):
    monkeypatch.setenv("QUERY_API_KEY", "query-key")
    c = _query_client()
    for i in range(_RATE_LIMIT):
        assert _ask_from(c, "not-an-ip").status_code == 200
    assert _ask_from(c, "also-bad").status_code == 429


def test_missing_forwarded_ip_falls_back_to_peer(monkeypatch):
    monkeypatch.setenv("QUERY_API_KEY", "query-key")
    c = _query_client()
    for _ in range(_RATE_LIMIT):
        assert _ask_from(c, None).status_code == 200
    assert _ask_from(c, None).status_code == 429


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
