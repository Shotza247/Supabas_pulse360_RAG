"""Small Supabase pgvector ingestion and retrieval smoke-test UI.

Run with:
    uv run streamlit run supabase_pgvector_test.py --server.port 8503

This is intentionally separate from streamlit_app.py, which exercises the
inherited Qdrant/SQLite baseline. It writes only to the selected test
collection and uses direct PostgreSQL so a private `rag` schema does not need
to be exposed through the Supabase REST API.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import streamlit as st
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from faq_agent.chunking.chunker import chunk_document  # noqa: E402
from faq_agent.config import get_settings  # noqa: E402
from faq_agent.embeddings.embedder import build_embedding_client  # noqa: E402
from faq_agent.schemas import SourceDocument  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

COLLECTION_ID = os.getenv("SUPABASE_TEST_COLLECTION", "test_collection")
TEST_FILENAME_PREFIX = "__pgvector_test__/"
MAX_BYTES = 2 * 1024 * 1024


def db_connection():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Install dependencies with `uv sync --extra ui` first.") from exc

    database_url = os.getenv("SUPABASE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("Set SUPABASE_DATABASE_URL in .env before starting the test UI.")
    return psycopg.connect(database_url)


def table_columns(connection, table_name: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select column_name
            from information_schema.columns
            where table_schema = 'rag' and table_name = %s
            """,
            (table_name,),
        )
        return {row[0] for row in cursor.fetchall()}


def list_collections(connection) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute("select id from rag.collections order by id")
        return [row[0] for row in cursor.fetchall()]


def require_collection(connection, collection_id: str = COLLECTION_ID) -> None:
    with connection.cursor() as cursor:
        cursor.execute("select 1 from rag.collections where id = %s", (collection_id,))
        if cursor.fetchone() is None:
            raise RuntimeError(
                f"Collection {collection_id!r} does not exist. Create it once in Supabase, "
                "then rerun this reusable test."
            )


def parse_upload(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        return data.decode("utf-8-sig")
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    raise ValueError("Use a PDF, TXT, or Markdown document.")


def build_chunks(filename: str, text: str, *, test_document: bool = True):
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    document = SourceDocument(
        source_id=digest,
        path=filename,
        title=Path(filename).stem,
        text=text,
        metadata={
            "filename": (TEST_FILENAME_PREFIX if test_document else "") + Path(filename).name,
            "page": None,
        },
    )
    return digest, chunk_document(
        document,
        max_chars=int(os.getenv("CHUNK_CHARS", "1000")),
        overlap_chars=int(os.getenv("CHUNK_OVERLAP", "150")),
    )


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.10g}" for value in vector) + "]"


def insert_document(
    connection,
    filename: str,
    category: str,
    text: str,
    *,
    collection_id: str = COLLECTION_ID,
    test_document: bool = True,
) -> dict[str, Any]:
    digest, chunks = build_chunks(filename, text, test_document=test_document)
    if not chunks:
        raise ValueError("The document contains no readable text.")
    settings = get_settings()
    embeddings = build_embedding_client(settings)
    vectors = embeddings.embed_documents([chunk.text for chunk in chunks])

    document_columns = table_columns(connection, "documents")
    chunk_columns = table_columns(connection, "document_chunks")
    required_document_columns = {"collection_id", "filename", "category", "status"}
    required_chunk_columns = {"document_id", "content", "embedding"}
    missing = (required_document_columns - document_columns) | (
        required_chunk_columns - chunk_columns
    )
    if missing:
        raise RuntimeError(
            "The Supabase schema is missing expected columns: " + ", ".join(sorted(missing))
        )

    stored_filename = (TEST_FILENAME_PREFIX if test_document else "") + Path(filename).name
    document_values: dict[str, Any] = {
        "collection_id": collection_id,
        "filename": stored_filename,
        "category": category,
        "status": "COMMITTED",
        "checksum": digest,
        "content_hash": digest,
        "embedding_model": settings.embedding_model,
        "embedding_dimensions": settings.vector_dimensions,
        "metadata": json.dumps({"test_document": True, "source_filename": filename}),
    }
    insert_columns = [column for column in document_values if column in document_columns]
    placeholders = ", ".join(["%s"] * len(insert_columns))
    document_sql = (
        f"insert into rag.documents ({', '.join(insert_columns)}) "
        f"values ({placeholders}) returning id"
    )

    with connection.cursor() as cursor:
        cursor.execute(document_sql, tuple(document_values[column] for column in insert_columns))
        document_id = cursor.fetchone()[0]

        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
            chunk_values: dict[str, Any] = {
                "document_id": document_id,
                "content": chunk.text,
                "chunk_index": index,
                "page_number": chunk.metadata.get("page"),
                "embedding": vector_literal(vector),
                "metadata": json.dumps(chunk.metadata),
            }
            chunk_insert_columns = [column for column in chunk_values if column in chunk_columns]
            chunk_placeholders = ", ".join(["%s"] * len(chunk_insert_columns))
            chunk_sql = (
                f"insert into rag.document_chunks ({', '.join(chunk_insert_columns)}) "
                f"values ({chunk_placeholders})"
            )
            cursor.execute(
                chunk_sql, tuple(chunk_values[column] for column in chunk_insert_columns)
            )
    connection.commit()
    return {"document_id": str(document_id), "filename": stored_filename, "chunks": len(chunks)}


def search_document(
    connection,
    question: str,
    category: str | None = None,
    *,
    collection_id: str = COLLECTION_ID,
) -> list[dict[str, Any]]:
    settings = get_settings()
    vector = build_embedding_client(settings).embed_query(question)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select chunk_id, document_id, filename, category, content, page_number, similarity
            from rag.match_document_chunks(%s::extensions.vector, %s, %s, 6, 0.0)
            """,
            (vector_literal(vector), collection_id, category),
        )
        columns = [description.name for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def main() -> None:
    st.set_page_config(page_title="Supabase pgvector smoke test", page_icon="🧪", layout="wide")
    st.title("Supabase pgvector smoke test")
    st.caption(
        f"Reusable test UI · collection: {COLLECTION_ID} · writes only test documents, "
        "never drops the collection"
    )

    with st.sidebar:
        st.subheader("Connection")
        st.code("SUPABASE_DATABASE_URL", language="text")
        st.info("Use the Supabase direct/session connection string. Keep it in .env only.")
        category = st.text_input("Test category", value="platform")

    upload = st.file_uploader("Upload a small test document", type=["pdf", "txt", "md"])
    if upload is None:
        st.info("Choose a small PDF, TXT, or Markdown file to preview and embed.")
        st.stop()
    data = upload.getvalue()
    if len(data) > MAX_BYTES:
        st.error("Keep smoke-test files at or below 2 MB.")
        st.stop()

    try:
        text = parse_upload(upload.name, data)
        digest, chunks = build_chunks(upload.name, text)
    except Exception as exc:
        st.error(str(exc))
        st.stop()
    if not text.strip() or not chunks:
        st.error("No readable text or chunks were produced.")
        st.stop()

    left, right = st.columns(2)
    left.metric("Characters", len(text))
    left.metric("Chunks", len(chunks))
    right.metric("SHA-256", digest[:16] + "…")
    right.metric("Embedding dimensions", get_settings().vector_dimensions)
    with st.expander("Preview extracted text and chunks"):
        st.text_area("Extracted text", text, height=180)
        for index, chunk in enumerate(chunks[:5], 1):
            st.markdown(f"**Chunk {index}**")
            st.write(chunk.text)

    if st.button("Embed and store in Supabase", type="primary"):
        try:
            with db_connection() as connection:
                require_collection(connection, COLLECTION_ID)
                result = insert_document(connection, upload.name, category, text)
            st.session_state["test_document"] = result
            st.success(f"Stored {result['chunks']} chunks in {COLLECTION_ID}.")
        except Exception as exc:
            st.error(str(exc))

    test_document = st.session_state.get("test_document")
    if test_document:
        st.divider()
        st.subheader("Test pgvector retrieval")
        question = st.text_input("Question for match_document_chunks", value="What is this document about?")
        if st.button("Search Supabase pgvector"):
            try:
                with db_connection() as connection:
                    matches = search_document(connection, question, category or None)
                st.write(f"{len(matches)} matching chunks")
                for match in matches:
                    with st.expander(f"{match['filename']} · similarity {match['similarity']:.4f}"):
                        st.write(match["content"])
            except Exception as exc:
                st.error(str(exc))
        st.warning(
            "When finished, run scripts/delete_supabase_test_document.py. "
            "It removes only this test document and its chunks/embeddings."
        )


if __name__ == "__main__":
    main()
