"""Small Supabase pgvector ingestion and retrieval smoke-test UI.

Run with:
    uv run streamlit run supabase_pgvector_test.py --server.port 8503

This is intentionally separate from streamlit_app.py, which is the production
workspace UI.  It writes only to the selected test collection and uses direct
PostgreSQL so the private `rag` schema does not need to be exposed via the
Supabase REST API.

Shared DB helpers now live in ``faq_agent.db_helpers`` (finding N5) so that
changes here cannot silently affect the production UI.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

load_dotenv(PROJECT_ROOT / ".env")

from faq_agent.config import get_settings  # noqa: E402
from faq_agent.db_helpers import (  # noqa: E402
    build_chunks,
    db_connection,
    insert_document,
    parse_upload,
    require_collection,
    search_document,
)

COLLECTION_ID = os.getenv("SUPABASE_TEST_COLLECTION", "test_collection")
MAX_BYTES = 2 * 1024 * 1024


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
                result = insert_document(connection, upload.name, category, text, collection_id=COLLECTION_ID)
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
                    matches = search_document(connection, question, category or None, collection_id=COLLECTION_ID)
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
