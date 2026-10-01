"""Local collection catalog and reviewed uploads; Qdrant stores document chunks."""

import hashlib
import io
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException
from qdrant_client import QdrantClient
from qdrant_client import models as m

from faq_agent.chunking.chunker import chunk_document
from faq_agent.config import PROJECT_ROOT
from faq_agent.embeddings.embedder import build_embedding_client, validate_vectors
from faq_agent.schemas import SourceDocument

ROOT = PROJECT_ROOT / ".local" / "library.sqlite3"
MAX_BYTES = 10 * 1024 * 1024


class Library:
    def __init__(self, settings, path=ROOT, client=None, embeddings=None):
        if settings.vector_store != "qdrant":
            raise HTTPException(409, "The upload library currently requires Qdrant")
        self.s, self.path = settings, Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.client = client or QdrantClient(
            url=settings.vector_url,
            api_key=settings.vector_api_key or None,
            timeout=settings.request_timeout,
        )
        self.embeddings = embeddings or build_embedding_client(settings)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS collections (
                    id TEXT PRIMARY KEY, physical TEXT UNIQUE, profile TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS documents (
                    collection_id TEXT, id TEXT, filename TEXT, chunks INTEGER, created REAL,
                    PRIMARY KEY(collection_id,id));
                CREATE TABLE IF NOT EXISTS previews (
                    id TEXT PRIMARY KEY, collection_id TEXT, document_id TEXT, filename TEXT,
                    chunks TEXT, created REAL, committed INTEGER DEFAULT 0);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=300)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self):
        self.client.close()

    def profile(self):
        return json.dumps(
            {
                k: getattr(self.s, k)
                for k in (
                    "vector_url",
                    "embedding_model",
                    "embedding_revision",
                    "vector_dimensions",
                    "embedding_query_prefix",
                    "embedding_document_prefix",
                    "chunk_chars",
                    "chunk_overlap",
                )
            },
            sort_keys=True,
        )

    def collection(self, name):
        with self.db() as db:
            row = db.execute("SELECT * FROM collections WHERE id=?", (name,)).fetchone()
        if not row:
            raise HTTPException(404, "Collection is not managed by this application")
        if row["profile"] != self.profile():
            raise HTTPException(409, "Collection embedding configuration differs from the backend")
        info = self.client.get_collection(row["physical"])
        vectors = info.config.params.vectors
        if (
            not isinstance(vectors, m.VectorParams)
            or vectors.size != self.s.vector_dimensions
            or vectors.distance != m.Distance.COSINE
        ):
            raise HTTPException(409, "Collection vector configuration is incompatible")
        return dict(row)

    def list_collections(self):
        with self.db() as db:
            rows = db.execute("SELECT id,physical,profile FROM collections ORDER BY id").fetchall()
        return [
            {
                "id": r["id"],
                "physical_name": r["physical"],
                "compatible": r["profile"] == self.profile(),
                "embedding_model": json.loads(r["profile"])["embedding_model"],
                "dimensions": json.loads(r["profile"])["vector_dimensions"],
            }
            for r in rows
        ]

    def create(self, name):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{2,47}", name):
            raise HTTPException(422, "Use 3-48 lowercase letters, digits, underscores or hyphens")
        physical = replace(self.s, collection="faq_ui_" + name).index_name
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM collections WHERE id=?", (name,)
            ).fetchone() or self.client.collection_exists(physical):
                raise HTTPException(409, "Collection already exists; choose another name")
            self.client.create_collection(
                physical,
                vectors_config=m.VectorParams(
                    size=self.s.vector_dimensions, distance=m.Distance.COSINE
                ),
            )
            db.execute(
                "INSERT INTO collections VALUES (?,?,?,?)",
                (name, physical, self.profile(), time.time()),
            )
        return {
            "id": name,
            "physical_name": physical,
            "dimensions": self.s.vector_dimensions,
            "embedding_model": self.s.embedding_model,
        }

    def documents(self, name):
        self.collection(name)
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,filename,chunks,created FROM documents WHERE collection_id=? ORDER BY created DESC",
                    (name,),
                )
            ]

    def preview(self, name, filename, data):
        self.collection(name)
        filename = filename.replace("\\", "/").split("/")[-1][:160]
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".txt", ".md"}:
            raise HTTPException(415, "Supported files: PDF, TXT and Markdown")
        if not data or len(data) > MAX_BYTES:
            raise HTTPException(413, "Upload must be nonempty and at most 10 MB")
        digest = hashlib.sha256(data).hexdigest()
        try:
            if suffix == ".pdf":
                from pypdf import PdfReader

                reader = PdfReader(io.BytesIO(data))
                if reader.is_encrypted or len(reader.pages) > 100:
                    raise ValueError("Encrypted or oversized PDF")
                pages = [(i + 1, page.extract_text() or "") for i, page in enumerate(reader.pages)]
            else:
                pages = [(None, data.decode("utf-8-sig"))]
            if sum(len(text) for _, text in pages) > 100_000:
                raise ValueError("Extracted text exceeds preview limit")
            chunks = []
            for page, text in pages:
                doc = SourceDocument(
                    digest + ":" + str(page),
                    "",
                    Path(filename).stem,
                    text,
                    {"filename": filename, "page": page},
                )
                chunks.extend(
                    asdict(c)
                    for c in chunk_document(
                        doc, max_chars=self.s.chunk_chars, overlap_chars=self.s.chunk_overlap
                    )
                )
            if not chunks or len(chunks) > 100:
                raise ValueError("No readable text or more than 100 chunks")
        except Exception as exc:
            raise HTTPException(
                422,
                "Cannot preview this file: use readable text, up to 100 pages/100 chunks; scanned PDFs need OCR",
            ) from exc
        token = uuid4().hex
        with self.db() as db:
            db.execute("DELETE FROM previews WHERE created<?", (time.time() - 3600,))
            if db.execute("SELECT count(*) FROM previews").fetchone()[0] >= 30:
                raise HTTPException(429, "Too many previews; wait for expiry")
            db.execute(
                "INSERT INTO previews VALUES (?,?,?,?,?,?,0)",
                (token, name, digest, filename, json.dumps(chunks), time.time()),
            )
            existing = bool(
                db.execute(
                    "SELECT 1 FROM documents WHERE collection_id=? AND id=?", (name, digest)
                ).fetchone()
            )
        return {
            "preview_id": token,
            "collection_id": name,
            "document_id": digest,
            "filename": filename,
            "chunks": chunks,
            "chunk_count": len(chunks),
            "dimensions": self.s.vector_dimensions,
            "embedding_model": self.s.embedding_model,
            "already_stored": existing,
            "expires_in_seconds": 3600,
        }

    def commit(self, name, token, approved):
        if not approved:
            raise HTTPException(422, "Approve hosted processing before embedding")
        collection = self.collection(name)
        # Serializing commits makes retries idempotent, including simultaneous button clicks.
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM previews WHERE id=? AND collection_id=?", (token, name)
            ).fetchone()
            if not row or row["created"] < time.time() - 3600:
                raise HTTPException(410, "Preview expired or does not belong to this collection")
            existing = db.execute(
                "SELECT * FROM documents WHERE collection_id=? AND id=?", (name, row["document_id"])
            ).fetchone()
            if existing:
                return {
                    "status": "already_stored",
                    "document_id": row["document_id"],
                    "chunks": existing["chunks"],
                }
            chunks = json.loads(row["chunks"])
            vectors = self.embeddings.embed_documents([c["text"] for c in chunks])
            validate_vectors(vectors, len(chunks), self.s.vector_dimensions)
            points = [
                m.PointStruct(
                    id=str(uuid5(NAMESPACE_URL, row["document_id"] + ":" + c["chunk_id"])),
                    vector=v,
                    payload={
                        **c["metadata"],
                        "chunk_id": c["chunk_id"],
                        "text": c["text"],
                        "document_id": row["document_id"],
                        "source_id": row["document_id"],
                    },
                )
                for c, v in zip(chunks, vectors, strict=True)
            ]
            self.client.upsert(collection["physical"], points=points, wait=True)
            db.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?)",
                (name, row["document_id"], row["filename"], len(chunks), time.time()),
            )
            db.execute("UPDATE previews SET committed=1 WHERE id=?", (token,))
        return {"status": "stored", "document_id": row["document_id"], "chunks": len(chunks)}

    def search(self, name, question, document_id=None):
        collection = self.collection(name)
        docs = self.documents(name)
        ids = [d["id"] for d in docs]
        if document_id is not None:
            if document_id not in ids:
                raise HTTPException(404, "Document is not in this collection")
            ids = [document_id]
        if not ids:
            return []
        vector = self.embeddings.embed_query(question)
        validate_vectors([vector], 1, self.s.vector_dimensions)
        results = self.client.query_points(
            collection["physical"],
            query=vector,
            query_filter=m.Filter(
                must=[m.FieldCondition(key="document_id", match=m.MatchAny(any=ids))]
            ),
            limit=self.s.context_k,
            with_payload=True,
        ).points
        return [
            {
                "chunk_id": p.payload["chunk_id"],
                "text": p.payload["text"],
                "title": p.payload.get("title", ""),
                "section": p.payload.get("section", ""),
                "filename": p.payload.get("filename"),
                "page": p.payload.get("page"),
                "document_id": p.payload["document_id"],
                "vector_score": p.score,
                "score": p.score,
                "rerank_score": None,
            }
            for p in results
        ]
