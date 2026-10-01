"""Supabase PostgreSQL catalog and pgvector storage for the FAQ API."""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from faq_agent.chunking.chunker import chunk_document
from faq_agent.embeddings.embedder import build_embedding_client, validate_vectors
from faq_agent.schemas import SourceDocument

MAX_BYTES = 10 * 1024 * 1024
_PREVIEWS: dict[str, dict[str, Any]] = {}


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.10g}" for value in vector) + "]"


class SupabaseLibrary:
    def __init__(self, settings):
        self.s = settings
        self.database_url = settings.supabase_database_url or os.getenv("DATABASE_URL", "")
        if not self.database_url:
            raise HTTPException(503, "SUPABASE_DATABASE_URL is not configured")
        try:
            import psycopg
        except ImportError as exc:
            raise HTTPException(503, "Install the psycopg dependency for Supabase support") from exc
        self.psycopg = psycopg

    @contextmanager
    def connection(self):
        try:
            with self.psycopg.connect(self.database_url) as connection:
                yield connection
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "Supabase database connection failed") from exc

    @staticmethod
    def columns(connection, table: str) -> set[str]:
        with connection.cursor() as cursor:
            cursor.execute(
                "select column_name from information_schema.columns "
                "where table_schema='rag' and table_name=%s",
                (table,),
            )
            return {row[0] for row in cursor.fetchall()}

    def require_collection(self, collection_id: str):
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute("select 1 from rag.collections where id=%s", (collection_id,))
            if cursor.fetchone() is None:
                raise HTTPException(404, "Collection is not registered in Supabase")

    def list_collections(self):
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute("select id from rag.collections order by id")
            return [{"id": row[0], "dimensions": self.s.vector_dimensions, "embedding_model": self.s.embedding_model, "compatible": True} for row in cursor.fetchall()]

    def create(self, name: str):
        if not name or not name.isascii() or not name.replace("_", "").replace("-", "").isalnum() or not name[0].islower():
            raise HTTPException(422, "Use a lowercase collection identifier")
        with self.connection() as connection, connection.cursor() as cursor:
            try:
                cursor.execute("insert into rag.collections (id) values (%s) returning id", (name,))
                created = cursor.fetchone()[0]
            except Exception as exc:
                connection.rollback()
                raise HTTPException(409, "Collection could not be created; check its schema or existing ID") from exc
        return {"id": created, "dimensions": self.s.vector_dimensions, "embedding_model": self.s.embedding_model, "compatible": True}

    def documents(self, collection_id: str):
        self.require_collection(collection_id)
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                select d.id, d.filename, d.category, d.status, count(c.id)::int as chunks
                from rag.documents d left join rag.document_chunks c on c.document_id=d.id
                where d.collection_id=%s and d.status='COMMITTED'
                group by d.id, d.filename, d.category, d.status order by d.filename
                """,
                (collection_id,),
            )
            columns = [description.name for description in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def preview(self, collection_id: str, filename: str, data: bytes):
        self.require_collection(collection_id)
        filename = Path(filename.replace("\\", "/")).name[:160]
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".txt", ".md"}:
            raise HTTPException(415, "Supported files: PDF, TXT and Markdown")
        if not data or len(data) > MAX_BYTES:
            raise HTTPException(413, "Upload must be nonempty and at most 10 MB")
        try:
            if suffix == ".pdf":
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(data))
                pages = [(index + 1, page.extract_text() or "") for index, page in enumerate(reader.pages)]
            else:
                pages = [(None, data.decode("utf-8-sig"))]
            digest = hashlib.sha256(data).hexdigest()
            chunks = []
            for page, text in pages:
                document = SourceDocument(digest + ":" + str(page), filename, Path(filename).stem, text, {"filename": filename, "page": page})
                chunks.extend(chunk_document(document, max_chars=self.s.chunk_chars, overlap_chars=self.s.chunk_overlap))
            if not chunks or len(chunks) > 100:
                raise ValueError("No readable text or too many chunks")
        except Exception as exc:
            raise HTTPException(422, "Cannot preview this file; use a readable PDF, TXT, or Markdown document") from exc
        token = uuid4().hex
        _PREVIEWS[token] = {"collection_id": collection_id, "filename": filename, "digest": digest, "chunks": chunks, "created": time.time()}
        return {"preview_id": token, "collection_id": collection_id, "document_id": digest, "filename": filename, "chunks": [chunk.__dict__ for chunk in chunks], "chunk_count": len(chunks), "dimensions": self.s.vector_dimensions, "embedding_model": self.s.embedding_model, "already_stored": False, "expires_in_seconds": 3600}

    def commit(self, collection_id: str, token: str, approved: bool, category: str = "platform"):
        if not approved:
            raise HTTPException(422, "Approve hosted processing before embedding")
        preview = _PREVIEWS.get(token)
        if not preview or preview["collection_id"] != collection_id or preview["created"] < time.time() - 3600:
            raise HTTPException(410, "Preview expired or does not belong to this collection")
        chunks = preview["chunks"]
        with self.connection() as connection, connection.cursor() as cursor:
            document_columns = self.columns(connection, "documents")
            chunk_columns = self.columns(connection, "document_chunks")
            document_required = {"collection_id", "filename", "category", "status"}
            chunk_required = {"document_id", "content", "embedding"}
            missing = (document_required - document_columns) | (chunk_required - chunk_columns)
            if missing:
                raise HTTPException(500, "Supabase rag schema is missing: " + ", ".join(sorted(missing)))
            existing = None
            if "checksum" in document_columns:
                cursor.execute(
                    "select id from rag.documents where collection_id=%s and checksum=%s",
                    (collection_id, preview["digest"]),
                )
                existing = cursor.fetchone()
            if existing:
                return {"status": "already_stored", "document_id": str(existing[0]), "chunks": 0}
            vectors = build_embedding_client(self.s).embed_documents([chunk.text for chunk in chunks])
            validate_vectors(vectors, len(chunks), self.s.vector_dimensions)
            values = {"collection_id": collection_id, "filename": preview["filename"], "category": category, "status": "COMMITTED", "checksum": preview["digest"], "content_hash": preview["digest"], "embedding_model": self.s.embedding_model, "embedding_dimensions": self.s.vector_dimensions, "metadata": json.dumps({"source": "fastapi_upload"})}
            columns = [column for column in values if column in document_columns]
            cursor.execute(f"insert into rag.documents ({', '.join(columns)}) values ({', '.join(['%s'] * len(columns))}) returning id", tuple(values[column] for column in columns))
            document_id = cursor.fetchone()[0]
            for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                chunk_values = {"document_id": document_id, "content": chunk.text, "chunk_index": index, "page_number": chunk.metadata.get("page"), "embedding": vector_literal(vector), "metadata": json.dumps(chunk.metadata)}
                columns = [column for column in chunk_values if column in chunk_columns]
                cursor.execute(f"insert into rag.document_chunks ({', '.join(columns)}) values ({', '.join(['%s'] * len(columns))})", tuple(chunk_values[column] for column in columns))
        _PREVIEWS.pop(token, None)
        return {"status": "stored", "document_id": str(document_id), "chunks": len(chunks)}

    def search(self, collection_id: str, question: str, document_id: str | None = None):
        self.require_collection(collection_id)
        vector = build_embedding_client(self.s).embed_query(question)
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute("select * from rag.match_document_chunks(%s::extensions.vector, %s, null, %s, 0.0)", (vector_literal(vector), collection_id, self.s.context_k))
            columns = [description.name for description in cursor.description]
            rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        if document_id:
            rows = [row for row in rows if str(row["document_id"]) == document_id]
        return [{"chunk_id": str(row["chunk_id"]), "text": row["content"], "title": row["filename"], "section": "", "filename": row["filename"], "page": row["page_number"], "document_id": str(row["document_id"]), "vector_score": row["similarity"], "score": row["similarity"], "rerank_score": None} for row in rows]
