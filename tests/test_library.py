from dataclasses import replace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from faq_agent.api.documents import get_library
from faq_agent.api.routes import create_app
from faq_agent.config import Settings
from faq_agent.ingestion.service import Library


class Embeddings:
    def __init__(self):
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += 1
        return [[1.0, 0.2, 0.1] for _ in texts]

    def embed_query(self, text):
        self.calls += 1
        return [1.0, 0.2, 0.1]


@pytest.fixture
def library(tmp_path):
    lib = Library(
        Settings(vector_dimensions=3),
        path=tmp_path / "library.sqlite3",
        client=QdrantClient(":memory:"),
        embeddings=Embeddings(),
    )
    lib.create("first")
    lib.create("second")
    yield lib
    lib.close()


def test_preview_approval_and_idempotence(library):
    preview = library.preview("first", "sample.md", b"# FAQ\nProject Atlas displays rainfall.")
    assert library.embeddings.calls == 0
    assert library.documents("first") == []
    assert library.search("first", "rainfall?") == []
    with pytest.raises(HTTPException) as error:
        library.commit("first", preview["preview_id"], False)
    assert error.value.status_code == 422
    assert library.embeddings.calls == 0
    result = library.commit("first", preview["preview_id"], True)
    assert result["status"] == "stored"
    assert library.commit("first", preview["preview_id"], True)["status"] == "already_stored"
    assert library.embeddings.calls == 1
    hits = library.search("first", "rainfall?")
    assert len(hits) == 1
    assert hits[0]["filename"] == "sample.md"
    assert hits[0]["rerank_score"] is None
    assert library.search("second", "rainfall?") == []


def test_binding_filters_and_duplicates(library):
    a = library.preview("first", "a.txt", b"Alpha FAQ answer.")
    b = library.preview("first", "b.txt", b"Beta FAQ answer.")
    with pytest.raises(HTTPException):
        library.commit("second", a["preview_id"], True)
    library.commit("first", a["preview_id"], True)
    library.commit("first", b["preview_id"], True)
    assert len(library.search("first", "FAQ?", a["document_id"])) == 1
    with pytest.raises(HTTPException):
        library.search("second", "FAQ?", a["document_id"])
    duplicate = library.preview("first", "renamed.txt", b"Alpha FAQ answer.")
    assert duplicate["already_stored"]
    assert library.commit("first", duplicate["preview_id"], True)["status"] == "already_stored"
    assert library.client.get_collection(library.collection("first")["physical"]).points_count == 2


def test_validation_and_expiry(library):
    for filename, content, status in [
        ("x.exe", b"hi", 415),
        ("x.txt", b"", 413),
        ("x.pdf", b"broken", 422),
    ]:
        with pytest.raises(HTTPException) as error:
            library.preview("first", filename, content)
        assert error.value.status_code == status
    with pytest.raises(HTTPException):
        library.create("first")
    with pytest.raises(HTTPException):
        library.create("../bad")
    preview = library.preview("first", "a.md", b"Some text")
    with library.db() as db:
        db.execute("UPDATE previews SET created=0")
    with pytest.raises(HTTPException) as error:
        library.commit("first", preview["preview_id"], True)
    assert error.value.status_code == 410
    library.s = replace(library.s, embedding_model="another/model")
    with pytest.raises(HTTPException) as error:
        library.search("first", "Hi?")
    assert error.value.status_code == 409


def test_api_upload_and_scope(library):
    app = create_app(searcher=lambda q, c, d: library.search(c, q, d))
    app.dependency_overrides[get_library] = lambda: library
    client = TestClient(app)
    assert len(client.get("/collections").json()) == 2
    response = client.post(
        "/collections/first/uploads/preview", files={"file": ("faq.md", b"FAQ example answer.")}
    )
    assert response.status_code == 200
    preview = response.json()
    assert (
        client.post(
            "/collections/first/documents",
            json={"preview_id": preview["preview_id"], "approved": False},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/collections/first/documents",
            json={"preview_id": preview["preview_id"], "approved": True},
        ).status_code
        == 200
    )
    result = client.post("/search", json={"question": "FAQ?", "collection_id": "first"})
    assert result.status_code == 200
    assert result.json()["matches"][0]["document_id"] == preview["document_id"]
    assert (
        client.post("/ask", json={"question": "FAQ?", "document_id": "a" * 64}).status_code == 422
    )
    assert (
        client.post("/search", json={"question": "FAQ?", "collection_id": "missing"}).status_code
        == 404
    )


def test_ask_uses_scoped_shared_search(monkeypatch):
    from faq_agent.retrieval import retriever as api

    calls = []
    monkeypatch.setattr(api, "search_question", lambda *args: calls.append(args) or [])
    result = api.answer_question("Question?", "first", "a" * 64)
    assert calls == [("Question?", "first", "a" * 64)]
    assert result["status"] == "insufficient_evidence"


def test_partial_storage_not_searchable(library, monkeypatch):
    preview = library.preview("first", "faq.txt", b"Document answer.")
    upsert = library.client.upsert

    def fail_after_upsert(*args, **kwargs):
        upsert(*args, **kwargs)
        raise RuntimeError("simulated interrupted commit")

    monkeypatch.setattr(library.client, "upsert", fail_after_upsert)
    with pytest.raises(RuntimeError):
        library.commit("first", preview["preview_id"], True)
    assert library.search("first", "Question?") == []
    monkeypatch.setattr(library.client, "upsert", upsert)
    library.commit("first", preview["preview_id"], True)
    assert len(library.search("first", "Question?")) == 1
