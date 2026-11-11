"""Live OpenAI-embedding + Supabase pgvector test.

Skipped unless RUN_LIVE_TESTS=1 and the real credentials are in .env:

    $env:RUN_LIVE_TESTS = "1"; uv run pytest tests/test_live_openai_pgvector.py -v

It spends a few OpenAI embedding calls and writes to Supabase, but only inside a
throwaway collection (ci_openai_<random>) that is created and removed by this module.
It never touches pulse360_faq, test_collection or any other collection, so it cannot
clash with real documents (rag.documents is UNIQUE on collection_id + sha256, and
rag.document_chunks is UNIQUE on document_id + ordinal).
"""

import os
import uuid

import pytest

from faq_agent.config import get_settings
from faq_agent.supabase_store import SupabaseLibrary

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to run"),
]


@pytest.fixture(scope="module")
def settings():
    s = get_settings()
    if s.embedding_provider != "openai" or not s.openai_api_key or not s.supabase_database_url:
        pytest.skip("needs EMBEDDING_PROVIDER=openai, OPENAI_API_KEY and SUPABASE_DATABASE_URL")
    return s


@pytest.fixture(scope="module")
def library(settings):
    return SupabaseLibrary(settings)


@pytest.fixture(scope="module")
def collection(library):
    """A private throwaway collection, always removed afterwards."""
    name = "ci_openai_" + uuid.uuid4().hex[:10]
    library.create(name)
    try:
        yield name
    finally:
        with library.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "delete from rag.document_chunks where document_id in "
                "(select id from rag.documents where collection_id=%s)",
                (name,),
            )
            cursor.execute("delete from rag.documents where collection_id=%s", (name,))
            cursor.execute("delete from rag.previews where collection_id=%s", (name,))
            cursor.execute("delete from rag.collections where id=%s", (name,))


@pytest.fixture(scope="module")
def marker():
    return "QZ" + uuid.uuid4().hex[:8].upper()


@pytest.fixture(scope="module")
def document(marker):
    """Several distinct paragraphs so the chunker yields more than one chunk."""
    topics = [
        (f"Reference {marker}. The Pulse360 support desk opens at 07:45 on weekdays and closes "
         "at 17:30. Tickets raised outside those hours are answered the next working morning."),
        ("Reviewer nominations: every employee nominates between three and five reviewers "
         "during each feedback cycle. Managers approve the final list before invitations go out."),
        ("Data retention: raw survey responses are anonymised after ninety days. Aggregated "
         "reports are kept for three years and can be exported by a System Admin at any time."),
        ("Billing: invoices are issued on the first of each month in the customer's local "
         "currency. Payment terms are thirty days from the invoice date."),
    ]
    return "\n\n".join(t + " " + "Additional detail follows. " * 18 for t in topics).encode()


def _scalar(library, sql, params=()):
    with library.connection() as connection, connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()[0]


def test_schema_contract_matches_what_the_store_writes(library):
    """Fail early if the live schema drifts from the columns the code depends on."""
    with library.connection() as connection:
        documents = library.columns(connection, "documents")
        chunks = library.columns(connection, "document_chunks")
    assert {"collection_id", "filename", "category", "status", "sha256", "metadata"} <= documents
    assert {"document_id", "ordinal", "content", "embedding", "page_number", "metadata"} <= chunks
    column_type = _scalar(
        library,
        "select format_type(a.atttypid, a.atttypmod) from pg_attribute a "
        "join pg_class t on t.oid=a.attrelid "
        "where t.relnamespace='rag'::regnamespace and t.relname='document_chunks' "
        "and a.attname='embedding'",
    )
    assert column_type == f"vector({get_settings().vector_dimensions})"
    assert _scalar(library, "select count(*) from pg_proc where proname='match_document_chunks'") == 1


def test_openai_embeddings_are_stored_and_searchable(library, collection, document, marker, settings):
    preview = library.preview(collection, "live_openai.txt", document)
    assert preview["embedding_model"] == settings.embedding_model
    assert preview["dimensions"] == settings.vector_dimensions
    assert preview["chunk_count"] >= 2

    stored = library.commit(collection, preview["preview_id"], True, "support")
    assert stored["status"] == "stored"
    assert stored["chunks"] == preview["chunk_count"]

    # Row-level facts the constraints and the match function rely on.
    row = {}
    with library.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "select d.status, d.category, d.sha256, count(k.id), min(vector_dims(k.embedding)), "
            "max(vector_dims(k.embedding)), min(k.ordinal), max(k.ordinal), "
            "count(distinct k.ordinal) from rag.documents d join rag.document_chunks k "
            "on k.document_id=d.id where d.id=%s group by d.status, d.category, d.sha256",
            (stored["document_id"],),
        )
        (row["status"], row["category"], row["sha256"], row["chunks"], row["dmin"], row["dmax"],
         row["omin"], row["omax"], row["odistinct"]) = cursor.fetchone()
    assert (row["status"], row["category"]) == ("COMMITTED", "support")
    assert row["sha256"] == preview["document_id"]
    assert row["chunks"] == stored["chunks"]
    assert row["dmin"] == row["dmax"] == settings.vector_dimensions
    assert (row["omin"], row["omax"], row["odistinct"]) == (0, row["chunks"] - 1, row["chunks"])

    # The question is about the first paragraph, so the top hit must contain it.
    hits = library.search(collection, "When does the Pulse360 support desk open?")
    assert hits, "no matches returned"
    assert hits[0]["filename"] == "live_openai.txt"
    assert "07:45" in hits[0]["text"]
    assert hits[0]["vector_score"] > 0.4
    assert all(0.0 <= h["vector_score"] <= 1.0001 for h in hits)

    # A different topic must rank a different paragraph first.
    billing = library.search(collection, "How long do customers have to pay an invoice?")
    assert "thirty days" in billing[0]["text"]


def test_reingesting_the_same_file_does_not_clash(library, collection, document):
    before = _scalar(
        library,
        "select count(*) from rag.document_chunks k join rag.documents d on d.id=k.document_id "
        "where d.collection_id=%s",
        (collection,),
    )
    again = library.preview(collection, "renamed_copy.txt", document)
    result = library.commit(collection, again["preview_id"], True, "support")
    assert result["status"] == "already_stored"
    assert result["chunks"] == 0
    after = _scalar(
        library,
        "select count(*) from rag.document_chunks k join rag.documents d on d.id=k.document_id "
        "where d.collection_id=%s",
        (collection,),
    )
    assert after == before


def test_other_collections_are_never_modified(library, collection, settings):
    """The throwaway collection is the only one this module may have written to."""
    leaked = _scalar(
        library,
        "select count(*) from rag.documents where collection_id<>%s and filename "
        "in ('live_openai.txt','renamed_copy.txt')",
        (collection,),
    )
    assert leaked == 0
