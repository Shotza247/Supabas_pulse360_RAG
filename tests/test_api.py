import pytest
from fastapi.testclient import TestClient

from faq_agent.api.routes import create_app


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


def test_service_errors_documented():
    schema = client().get("/openapi.json").json()
    for path in ("/search", "/ask"):
        response = schema["paths"][path]["post"]["responses"]["503"]
        assert response["content"]["application/json"]["schema"]["$ref"].endswith(
            "/ServiceErrorResponse"
        )
