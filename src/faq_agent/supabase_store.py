"""Supabase PostgreSQL catalog and pgvector storage for the FAQ API."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from faq_agent.chunking.chunker import chunk_document
from faq_agent.embeddings.embedder import build_embedding_client, validate_vectors
from faq_agent.schemas import SourceDocument

logger = logging.getLogger("faq_agent")

MAX_BYTES = 10 * 1024 * 1024
_PREVIEW_TTL = 3600  # seconds; also encoded in the rag.previews table


# ---------------------------------------------------------------------------
# pgvector psycopg3 type adapter
# ---------------------------------------------------------------------------
# We register a custom Dumper so Python list[float] values are sent to
# PostgreSQL as the pgvector wire format ("[0.1,0.2,…]") through the normal
# psycopg3 parameterisation path — no raw string interpolation required.
#
# Why not use the `pgvector-python` package?
#   It would add a dependency that is not yet in pyproject.toml.  psycopg3's
#   Dumper API is stable and ships with the psycopg package we already depend
#   on, so this is zero-dependency.
#
# How it works:
#   _register_vector_dumper(conn) is called once per connection inside the
#   connection() context manager.  After that, any list[float] value passed
#   as a %s parameter is automatically serialised as "[f1,f2,…]" and sent
#   with the correct PostgreSQL type OID so pgvector recognises it.
# ---------------------------------------------------------------------------

def _register_vector_dumper(connection) -> None:
    """Register a per-connection pgvector Dumper for list[float].

    A fresh subclass is created for each connection so the ``oid``
    attribute is never shared across concurrent connections — avoids a
    race condition where two threads calling this function simultaneously
    would both mutate a single module-level class attribute.
    """
    from psycopg.adapt import Dumper

    with connection.cursor() as cur:
        cur.execute("select oid from pg_type where typname = 'vector'")
        row = cur.fetchone()
    if row is None:
        raise HTTPException(503, "pgvector extension is not installed in this database")
    vector_oid = row[0]

    class _VectorDumper(Dumper):
        oid = vector_oid

        def dump(self, vector):
            return ("[" + ",".join(f"{v:.10g}" for v in vector) + "]").encode()

        def quote(self, vector):
            # Used in COPY and literal contexts; wrap in single quotes so
            # PostgreSQL accepts it as a vector literal.
            return b"'" + self.dump(vector) + b"'"

    connection.adapters.register_dumper(list, _VectorDumper)


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

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    @contextmanager
    def connection(self):
        try:
            with self.psycopg.connect(self.database_url) as connection:
                # Register the list[float] → pgvector adapter once per connection
                # so every %s placeholder that receives a Python list is sent
                # through the type-safe path rather than raw string formatting.
                _register_vector_dumper(connection)
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

    def _ensure_previews_table(self, connection) -> None:
        """Create rag.previews on first use if it does not yet exist.

        This is a lightweight migration guard — it runs inside the caller's
        connection so no extra round-trip is needed.  The table is small and
        append-only; the DDL is idempotent via IF NOT EXISTS.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                """
                create table if not exists rag.previews (
                    id            text        primary key,
                    collection_id text        not null,
                    filename      text        not null,
                    digest        text        not null,
                    chunks        jsonb       not null,
                    created_at    timestamptz not null default now(),
                    committed     boolean     not null default false
                )
                """
            )

    def _expire_previews(self, connection) -> None:
        """Delete rows older than the TTL to keep the table small."""
        with connection.cursor() as cursor:
            cursor.execute(
                "delete from rag.previews where created_at < now() - interval '1 hour'"
            )

    def _load_preview(self, connection, token: str, collection_id: str) -> dict:
        """Return the preview row or raise 410.

        Filters on both ``id`` and ``collection_id`` in the same SQL query so
        that a caller who supplies a valid token from a different collection
        gets the same 410 response as one who supplies a non-existent token.
        Checking collection membership in Python after fetching by id alone
        would allow a timing oracle: an attacker could probe whether a given
        preview_id exists in another collection by comparing response times.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                "select filename, digest, chunks, "
                "extract(epoch from created_at) as created "
                "from rag.previews where id = %s and collection_id = %s",
                (token, collection_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise HTTPException(410, "Preview expired or does not belong to this collection")
        filename, digest, chunks_json, created = row
        if float(created) < time.time() - _PREVIEW_TTL:
            raise HTTPException(410, "Preview expired or does not belong to this collection")
        return {
            "collection_id": collection_id,
            "filename": filename,
            "digest": digest,
            # chunks stored as JSONB — psycopg3 returns them already parsed
            "chunks": chunks_json if isinstance(chunks_json, list) else json.loads(chunks_json),
        }

    # ------------------------------------------------------------------ #
    # Collection management                                                #
    # ------------------------------------------------------------------ #

    def require_collection(self, collection_id: str) -> None:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute("select 1 from rag.collections where id=%s", (collection_id,))
            if cursor.fetchone() is None:
                raise HTTPException(404, "Collection is not registered in Supabase")

    def list_collections(self):
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute("select id from rag.collections order by id")
            return [
                {
                    "id": row[0],
                    "dimensions": self.s.vector_dimensions,
                    "embedding_model": self.s.embedding_model,
                    "compatible": True,
                }
                for row in cursor.fetchall()
            ]

    def create(self, name: str):
        if (
            not name
            or not name.isascii()
            or not name.replace("_", "").replace("-", "").isalnum()
            or not name[0].islower()
        ):
            raise HTTPException(422, "Use a lowercase collection identifier")
        with self.connection() as connection, connection.cursor() as cursor:
            try:
                cursor.execute(
                    "insert into rag.collections (id) values (%s) returning id", (name,)
                )
                created = cursor.fetchone()[0]
            except Exception as exc:
                connection.rollback()
                raise HTTPException(
                    409, "Collection could not be created; check its schema or existing ID"
                ) from exc
        return {
            "id": created,
            "dimensions": self.s.vector_dimensions,
            "embedding_model": self.s.embedding_model,
            "compatible": True,
        }

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
            cols = [description.name for description in cursor.description]
            return [dict(zip(cols, row, strict=True)) for row in cursor.fetchall()]

    # ------------------------------------------------------------------ #
    # Upload workflow: preview → commit                                    #
    # ------------------------------------------------------------------ #

    def preview(self, collection_id: str, filename: str, data: bytes):
        self.require_collection(collection_id)
        filename = Path(filename.replace("\\", "/")).name[:160]
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".txt", ".md"}:
            raise HTTPException(415, "Supported files: PDF, TXT and Markdown")
        if not data or len(data) > MAX_BYTES:
            raise HTTPException(413, "Upload must be nonempty and at most 10 MB")

        # ── Extract text and chunk locally (no embeddings called here) ──
        try:
            if suffix == ".pdf":
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(data))
                pages = [
                    (index + 1, page.extract_text() or "")
                    for index, page in enumerate(reader.pages)
                ]
            else:
                pages = [(None, data.decode("utf-8-sig"))]
            digest = hashlib.sha256(data).hexdigest()
            chunks = []
            for page, text in pages:
                document = SourceDocument(
                    digest + ":" + str(page),
                    filename,
                    Path(filename).stem,
                    text,
                    {"filename": filename, "page": page},
                )
                chunks.extend(
                    chunk_document(
                        document,
                        max_chars=self.s.chunk_chars,
                        overlap_chars=self.s.chunk_overlap,
                    )
                )
            if not chunks or len(chunks) > 100:
                raise ValueError("No readable text or too many chunks")
        except Exception as exc:
            raise HTTPException(
                422,
                "Cannot preview this file; use a readable PDF, TXT, or Markdown document",
            ) from exc

        # ── Persist preview in Supabase so it survives restarts ──
        token = uuid4().hex
        chunks_payload = [chunk.__dict__ for chunk in chunks]
        with self.connection() as connection:
            self._ensure_previews_table(connection)
            self._expire_previews(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "insert into rag.previews (id, collection_id, filename, digest, chunks) "
                    "values (%s, %s, %s, %s, %s)",
                    (token, collection_id, filename, digest, json.dumps(chunks_payload)),
                )

        logger.info(
            "preview_created collection=%s filename=%s digest=%s chunks=%d token=%s",
            collection_id,
            filename,
            digest,
            len(chunks),
            token,
        )
        return {
            "preview_id": token,
            "collection_id": collection_id,
            "document_id": digest,
            "filename": filename,
            "chunks": chunks_payload,
            "chunk_count": len(chunks),
            "dimensions": self.s.vector_dimensions,
            "embedding_model": self.s.embedding_model,
            "already_stored": False,
            "expires_in_seconds": _PREVIEW_TTL,
        }

    def commit(self, collection_id: str, token: str, approved: bool, category: str = "platform"):
        if not approved:
            raise HTTPException(422, "Approve hosted processing before embedding")

        with self.connection() as connection:
            self._ensure_previews_table(connection)
            preview = self._load_preview(connection, token, collection_id)

            # ── Validate schema columns before touching data ──
            # Check that every NOT NULL column we intend to write is actually
            # present in the live schema.  Silently skipping a column we supply
            # causes a cryptic DB NOT NULL error further down; raising here
            # gives operators an actionable message (see BUG_AUDIT 2026-10-02).
            document_columns = self.columns(connection, "documents")
            chunk_columns = self.columns(connection, "document_chunks")
            document_required = {"collection_id", "filename", "category", "status"}
            chunk_required = {"document_id", "content", "embedding"}
            missing = (document_required - document_columns) | (chunk_required - chunk_columns)
            if missing:
                raise HTTPException(
                    500, "Supabase rag schema is missing required columns: "
                    + ", ".join(sorted(missing))
                    + ". Run docs/migrations/ scripts to align the schema.",
                )
            # Warn loudly when an optional column we supply is absent — the
            # column will be skipped, but if the live schema made it NOT NULL
            # after the migration the INSERT will still fail at the DB layer.
            doc_optional = {"checksum", "content_hash", "embedding_model",
                            "embedding_dimensions", "metadata"}
            chunk_optional = {"chunk_index", "page_number", "metadata"}
            absent_optional = (
                (doc_optional - document_columns) | (chunk_optional - chunk_columns)
            )
            if absent_optional:
                logger.warning(
                    "commit_schema_mismatch collection=%s absent_optional_columns=%s "
                    "— columns will be skipped; verify schema if inserts fail",
                    collection_id,
                    sorted(absent_optional),
                )

            # ── C2 FIX: duplicate check BEFORE any embedding API call ──
            # N1 FIX: check sha256 first — that is the column name in the live
            # Supabase schema.  checksum and content_hash remain as fallbacks
            # for schemas that use those names instead.
            existing_id: str | None = None
            with connection.cursor() as cursor:
                for col in ("sha256", "checksum", "content_hash"):
                    if col in document_columns:
                        cursor.execute(
                            f"select id from rag.documents "
                            f"where collection_id=%s and {col}=%s",
                            (collection_id, preview["digest"]),
                        )
                        row = cursor.fetchone()
                        if row:
                            existing_id = str(row[0])
                        break  # found the right column; don't check further

            if existing_id:
                logger.info(
                    "commit_skipped_duplicate collection=%s digest=%s document_id=%s",
                    collection_id,
                    preview["digest"],
                    existing_id,
                )
                return {"status": "already_stored", "document_id": existing_id, "chunks": 0}

            # ── Embed only after confirming the document is new ──
            chunks = preview["chunks"]
            try:
                vectors = build_embedding_client(self.s).embed_documents(
                    [c["text"] for c in chunks]
                )
                validate_vectors(vectors, len(chunks), self.s.vector_dimensions)
            except Exception as exc:
                logger.error(
                    "commit_embed_failed collection=%s digest=%s error=%s",
                    collection_id,
                    preview["digest"],
                    type(exc).__name__,
                )
                raise

            # ── C3 FIX: explicit transaction — document + all chunks are one atomic unit ──
            # If any INSERT fails the entire transaction rolls back, leaving no orphaned rows.
            # The preview row is only marked committed after a clean write so that the
            # caller can safely retry by re-submitting the same preview_id.
            document_id: str | None = None
            try:
                with connection.transaction():
                    doc_values = {
                        "collection_id": collection_id,
                        "filename": preview["filename"],
                        "category": category,
                        "status": "COMMITTED",
                        "checksum": preview["digest"],
                        "content_hash": preview["digest"],
                        "embedding_model": self.s.embedding_model,
                        "embedding_dimensions": self.s.vector_dimensions,
                        "metadata": json.dumps({"source": "fastapi_upload"}),
                    }
                    doc_cols = [c for c in doc_values if c in document_columns]
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"insert into rag.documents ({', '.join(doc_cols)}) "
                            f"values ({', '.join(['%s'] * len(doc_cols))}) returning id",
                            tuple(doc_values[c] for c in doc_cols),
                        )
                        document_id = str(cursor.fetchone()[0])

                        for index, (chunk, vector) in enumerate(
                            zip(chunks, vectors, strict=True)
                        ):
                            chunk_values = {
                                "document_id": document_id,
                                "content": chunk["text"],
                                "chunk_index": index,
                                "page_number": chunk.get("metadata", {}).get("page"),
                                # Pass the raw list — VectorDumper serialises it
                                # as "[f1,f2,…]" via psycopg3 parameterisation.
                                "embedding": vector,
                                "metadata": json.dumps(chunk.get("metadata", {})),
                            }
                            chunk_cols = [c for c in chunk_values if c in chunk_columns]
                            cursor.execute(
                                f"insert into rag.document_chunks ({', '.join(chunk_cols)}) "
                                f"values ({', '.join(['%s'] * len(chunk_cols))})",
                                tuple(chunk_values[c] for c in chunk_cols),
                            )

                        # Mark preview as committed now that the write succeeded
                        cursor.execute(
                            "update rag.previews set committed = true where id = %s",
                            (token,),
                        )
            except Exception as exc:
                logger.error(
                    "commit_transaction_failed collection=%s digest=%s document_id=%s "
                    "error_type=%s error=%s",
                    collection_id,
                    preview["digest"],
                    document_id,  # may be None if the document INSERT itself failed
                    type(exc).__name__,
                    str(exc),
                )
                raise HTTPException(
                    500,
                    "Document storage failed; the transaction was rolled back. "
                    "No partial data was written. You may retry by re-submitting "
                    "the same preview_id.",
                ) from exc

        logger.info(
            "commit_stored collection=%s filename=%s document_id=%s chunks=%d",
            collection_id,
            preview["filename"],
            document_id,
            len(chunks),
        )
        return {"status": "stored", "document_id": document_id, "chunks": len(chunks)}

    # ------------------------------------------------------------------ #
    # Search                                                               #
    # ------------------------------------------------------------------ #

    def search(self, collection_id: str, question: str, document_id: str | None = None):
        self.require_collection(collection_id)
        vector = build_embedding_client(self.s).embed_query(question)
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                # Pass the list directly — VectorDumper on this connection
                # serialises it as the pgvector wire format via %s.
                "select * from rag.match_document_chunks"
                "(%s::extensions.vector, %s, null, %s, 0.0)",
                (vector, collection_id, self.s.context_k),
            )
            cols = [description.name for description in cursor.description]
            rows = [dict(zip(cols, row, strict=True)) for row in cursor.fetchall()]
        if document_id:
            rows = [row for row in rows if str(row["document_id"]) == document_id]
        return [
            {
                "chunk_id": str(row["chunk_id"]),
                "text": row["content"],
                "title": row["filename"],
                "section": "",
                "filename": row["filename"],
                "page": row["page_number"],
                "document_id": str(row["document_id"]),
                "vector_score": row["similarity"],
                "score": row["similarity"],
                "rerank_score": None,
            }
            for row in rows
        ]
