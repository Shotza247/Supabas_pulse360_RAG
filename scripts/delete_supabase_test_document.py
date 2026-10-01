"""Delete only reusable pgvector smoke-test data; never drop test_collection.

Usage:
    uv run python scripts/delete_supabase_test_document.py
    uv run python scripts/delete_supabase_test_document.py --document-id UUID
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

load_dotenv()

COLLECTION_ID = os.getenv("SUPABASE_TEST_COLLECTION", "test_collection")
PREFIX = "__pgvector_test__/"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-id", help="Delete one known test document UUID")
    args = parser.parse_args()

    try:
        import psycopg
    except ImportError as exc:
        raise SystemExit("Install dependencies with `uv sync --extra ui` first.") from exc

    database_url = os.getenv("SUPABASE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("Set SUPABASE_DATABASE_URL in .env first.")

    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        if args.document_id:
            cursor.execute(
                "select id from rag.documents where id = %s and collection_id = %s "
                "and filename like %s",
                (args.document_id, COLLECTION_ID, PREFIX + "%"),
            )
        else:
            cursor.execute(
                "select id from rag.documents where collection_id = %s and filename like %s",
                (COLLECTION_ID, PREFIX + "%"),
            )
        document_ids = [row[0] for row in cursor.fetchall()]
        if not document_ids:
            print("No reusable pgvector test documents found. Collection was preserved.")
            return

        cursor.execute(
            "delete from rag.document_chunks where document_id = any(%s)", (document_ids,)
        )
        chunks_deleted = cursor.rowcount
        cursor.execute("delete from rag.documents where id = any(%s)", (document_ids,))
        documents_deleted = cursor.rowcount

    print(
        f"Deleted {documents_deleted} test document(s) and {chunks_deleted} chunk(s)/embeddings "
        f"from {COLLECTION_ID}. The collection was not modified."
    )


if __name__ == "__main__":
    main()
