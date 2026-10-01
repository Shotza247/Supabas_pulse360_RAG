"""Remote Qdrant (default) and Chroma share this small adapter boundary."""

from typing import Protocol
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

from faq_agent.embeddings.embedder import validate_vectors
from faq_agent.schemas import Chunk, RetrievedChunk


class VectorStore(Protocol):
    def version_exists(self) -> bool: ...
    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None: ...
    def search(self, vector: list[float], top_k: int) -> list[RetrievedChunk]: ...
    def close(self) -> None: ...


def payload(chunk, version):
    return {
        "text": chunk.text,
        "source_id": chunk.source_id,
        "corpus_version": version,
        "title": str(chunk.metadata.get("title", "")),
        "section": str(chunk.metadata.get("section", "")),
        "chunk_id": chunk.chunk_id,
    }


class QdrantVectorStore:
    def __init__(self, settings, client=None):
        from qdrant_client import QdrantClient

        self.s = settings
        self.client = client or QdrantClient(
            url=settings.vector_url,
            api_key=settings.vector_api_key or None,
            timeout=settings.request_timeout,
        )

    def version_exists(self):
        from qdrant_client import models as m

        if not self.client.collection_exists(self.s.index_name):
            return False
        return (
            self.client.count(
                self.s.index_name,
                exact=True,
                count_filter=m.Filter(
                    must=[
                        m.FieldCondition(
                            key="corpus_version", match=m.MatchValue(value=self.s.corpus_version)
                        )
                    ]
                ),
            ).count
            > 0
        )

    def upsert(self, chunks, vectors):
        from qdrant_client import models as m

        validate_vectors(vectors, len(chunks), self.s.vector_dimensions)
        name = self.s.index_name
        if not self.client.collection_exists(name):
            self.client.create_collection(
                name,
                vectors_config=m.VectorParams(
                    size=self.s.vector_dimensions, distance=m.Distance.COSINE
                ),
            )
        self.client.create_payload_index(name, "corpus_version", m.PayloadSchemaType.KEYWORD)
        for start in range(0, len(chunks), 64):
            points = [
                m.PointStruct(
                    id=str(uuid5(NAMESPACE_URL, self.s.corpus_version + ":" + c.chunk_id)),
                    vector=v,
                    payload=payload(c, self.s.corpus_version),
                )
                for c, v in zip(
                    chunks[start : start + 64], vectors[start : start + 64], strict=True
                )
            ]
            self.client.upsert(name, points=points, wait=True)

    def search(self, vector, top_k):
        from qdrant_client import models as m

        validate_vectors([vector], 1, self.s.vector_dimensions)
        result = self.client.query_points(
            self.s.index_name,
            query=vector,
            limit=top_k,
            with_payload=True,
            query_filter=m.Filter(
                must=[
                    m.FieldCondition(
                        key="corpus_version", match=m.MatchValue(value=self.s.corpus_version)
                    )
                ]
            ),
        )
        return [
            RetrievedChunk(
                p.payload["chunk_id"],
                p.payload["text"],
                p.score,
                {k: v for k, v in p.payload.items() if k != "text"},
            )
            for p in result.points
        ]

    def close(self):
        self.client.close()


class ChromaVectorStore:
    def __init__(self, settings, client=None):
        self.s = settings
        if client is None:
            import chromadb

            url = urlparse(settings.vector_url)
            if url.path not in ("", "/") or url.scheme not in ("http", "https") or not url.hostname:
                raise ValueError("Chroma VECTOR_URL must be an HTTP(S) server origin")
            headers = (
                {"Authorization": f"Bearer {settings.vector_api_key}"}
                if settings.vector_api_key
                else None
            )
            client = chromadb.HttpClient(
                host=url.hostname,
                port=url.port or (443 if url.scheme == "https" else 80),
                ssl=url.scheme == "https",
                headers=headers,
            )
        self.client = client

    def version_exists(self):
        names = [
            item if isinstance(item, str) else item.name for item in self.client.list_collections()
        ]
        if self.s.index_name not in names:
            return False
        collection = self.client.get_collection(self.s.index_name, embedding_function=None)
        return bool(
            collection.get(where={"corpus_version": self.s.corpus_version}, limit=1, include=[])[
                "ids"
            ]
        )

    def upsert(self, chunks, vectors):
        validate_vectors(vectors, len(chunks), self.s.vector_dimensions)
        collection = self.client.get_or_create_collection(
            self.s.index_name, embedding_function=None, metadata={"hnsw:space": "cosine"}
        )
        for start in range(0, len(chunks), 64):
            batch = chunks[start : start + 64]
            collection.upsert(
                ids=[self.s.corpus_version + ":" + c.chunk_id for c in batch],
                embeddings=vectors[start : start + 64],
                documents=[c.text for c in batch],
                metadatas=[
                    {k: v for k, v in payload(c, self.s.corpus_version).items() if k != "text"}
                    for c in batch
                ],
            )

    def search(self, vector, top_k):
        validate_vectors([vector], 1, self.s.vector_dimensions)
        collection = self.client.get_collection(self.s.index_name, embedding_function=None)
        result = collection.query(
            query_embeddings=[vector],
            n_results=top_k,
            where={"corpus_version": self.s.corpus_version},
            include=["documents", "metadatas", "distances"],
        )
        return [
            RetrievedChunk(meta["chunk_id"], text, 1.0 - distance, meta)
            for text, meta, distance in zip(
                result["documents"][0], result["metadatas"][0], result["distances"][0], strict=True
            )
        ]

    def close(self):
        pass


def build_vector_store(settings):
    return (
        QdrantVectorStore(settings)
        if settings.vector_store == "qdrant"
        else ChromaVectorStore(settings)
    )
