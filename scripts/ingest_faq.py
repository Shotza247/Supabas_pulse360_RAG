"""Ingest a reviewed corpus into a new version, then explicitly promote it."""

import argparse
import json
from dataclasses import replace
from pathlib import Path

from faq_agent.chunking.chunker import chunk_document
from faq_agent.config import get_settings
from faq_agent.embeddings.embedder import build_embedding_client
from faq_agent.ingestion.loader import load_documents
from faq_agent.vectordb.vector_store import build_vector_store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--version", required=True, help="New, unique corpus version")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect loading/chunking locally; no model calls or database writes",
    )
    parser.add_argument(
        "--approved-public",
        action="store_true",
        help="Confirm sources are approved for public answers and hosted processing",
    )
    args = parser.parse_args()
    if not args.dry_run and not args.approved_public:
        parser.error("Review the corpus, then supply --approved-public")
    active = get_settings()
    if not args.dry_run and args.version == active.corpus_version:
        parser.error("Use a new version; never overwrite the active corpus")
    settings = replace(active, corpus_version=args.version)
    docs = load_documents(args.source)
    if not docs:
        parser.error("No supported documents found")
    chunks = [
        chunk
        for doc in docs
        for chunk in chunk_document(
            doc, max_chars=settings.chunk_chars, overlap_chars=settings.chunk_overlap
        )
    ]
    if not chunks:
        parser.error("No chunks found")
    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "documents": len(docs),
                    "chunks": len(chunks),
                    "source_characters": sum(len(doc.text) for doc in docs),
                    "largest_chunk_characters": max(len(chunk.text) for chunk in chunks),
                    "embedding_dimensions": settings.vector_dimensions,
                    "model_calls": 0,
                    "database_writes": 0,
                    "sources": [{"source_id": doc.source_id, "title": doc.title} for doc in docs],
                },
                indent=2,
            )
        )
        return
    embeddings = build_embedding_client(settings)
    store = build_vector_store(settings)
    try:
        if store.version_exists():
            parser.error("This corpus version already exists; choose a new unique version")
        for start in range(0, len(chunks), 64):
            batch = chunks[start : start + 64]
            store.upsert(batch, embeddings.embed_documents([c.text for c in batch]))
    finally:
        store.close()
    print(f"Staged {len(chunks)} chunks from {len(docs)} documents.")
    print(f"Index: {settings.index_name}; staged corpus: {settings.corpus_version}")
    for doc in docs:
        print(f"Source label for evaluation: {doc.source_id} ({doc.title})")
    print("Evaluate before setting CORPUS_VERSION on the API.")
    print("After a failed import or changed source files, retry with a fresh version.")


if __name__ == "__main__":
    main()
