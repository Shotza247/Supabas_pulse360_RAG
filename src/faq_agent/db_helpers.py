"""Shared helpers for direct-PostgreSQL access to the Supabase `rag` schema.

Both ``streamlit_app.py`` and ``supabase_pgvector_test.py`` use these
utilities.  Keeping them here (finding N5) means changes to the smoke-test
script never silently affect the production UI, and vice-versa.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from faq_agent.chunking.chunker import chunk_document
from faq_agent.config import get_settings
from faq_agent.embeddings.embedder import build_embedding_client
from faq_agent.schemas import SourceDocument
from faq_agent.supabase_store import _register_vector_dumper

TEST_FILENAME_PREFIX = "__pgvector_test__/"


@contextmanager
def db_connection():
    """Yield a psycopg3 connection with the pgvector type adapter registered.

    Registers the same per-connection list[float] → pgvector Dumper used by
    SupabaseLibrary so that any list[float] passed as a %s parameter is
    serialised through psycopg3's normal parameterisation path — no raw
    string interpolation via vector_literal() required.
    """
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Install dependencies with `uv sync --extra ui` first.") from exc

    database_url = os.getenv("SUPABASE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("Set SUPABASE_DATABASE_URL in .env before starting the UI.")
    with psycopg.connect(database_url) as connection:
        _register_vector_dumper(connection)
        yield connection


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


def require_collection(connection, collection_id: str) -> None:
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


def insert_document(
    connection,
    filename: str,
    category: str,
    text: str,
    *,
    collection_id: str,
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
        "sha256": digest,
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
                "ordinal": index,
                "page_number": chunk.metadata.get("page"),
                "embedding": vector,
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
    collection_id: str,
) -> list[dict[str, Any]]:
    settings = get_settings()
    vector = build_embedding_client(settings).embed_query(question)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select chunk_id, document_id, filename, category, content, page_number, similarity
            from rag.match_document_chunks(%s::extensions.vector, %s, %s, 6, 0.0)
            """,
            (vector, collection_id, category),
        )
        columns = [description.name for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
