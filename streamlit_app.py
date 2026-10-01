"""Supabase PostgreSQL + pgvector workspace.

Run with:
    uv run streamlit run streamlit_app.py --server.port 8502
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
load_dotenv(PROJECT_ROOT / ".env")

from faq_agent.config import get_settings  # noqa: E402
from faq_agent.llm.client import synthesize  # noqa: E402
from supabase_pgvector_test import (  # noqa: E402
    build_chunks,
    db_connection,
    insert_document,
    list_collections,
    parse_upload,
    require_collection,
    search_document,
)


def list_documents(connection, collection_id: str) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select d.id, d.filename, d.category, d.status, count(c.id)::int as chunks
            from rag.documents d
            left join rag.document_chunks c on c.document_id = d.id
            where d.collection_id = %s and d.status = 'COMMITTED'
            group by d.id, d.filename, d.category, d.status
            order by d.filename
            """,
            (collection_id,),
        )
        columns = [description.name for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def render_sources(items: list[dict]) -> None:
    for index, hit in enumerate(items, 1):
        label = hit.get("filename") or hit.get("title") or "Document"
        similarity = hit.get("similarity", hit.get("vector_score"))
        with st.expander(f"Source {index} | {label}"):
            if similarity is not None:
                st.metric("Similarity", f"{float(similarity):.4f}")
            st.write(hit.get("content", hit.get("text", "")))
            st.caption(f"Chunk: {hit.get('chunk_id')} · Document: {hit.get('document_id')}")


def answer_from_matches(question: str, matches: list[dict]):
    normalized = [
        {
            "chunk_id": item["chunk_id"],
            "text": item["content"],
            "filename": item.get("filename"),
            "page": item.get("page_number"),
            "document_id": item.get("document_id"),
            "vector_score": item.get("similarity"),
            "score": item.get("similarity"),
            "rerank_score": None,
            "title": item.get("filename", ""),
            "section": "",
        }
        for item in matches
    ]
    try:
        result = synthesize(question, normalized, get_settings())
        return result, normalized
    except Exception as error:
        return {
            "answer": "Retrieval succeeded, but answer synthesis is not configured yet: "
            + str(error),
            "status": "retrieved_without_synthesis",
            "sources": [],
        }, normalized


def main() -> None:
    st.set_page_config(
        page_title="Supabase pgvector workspace",
        page_icon=":material/database:",
        layout="wide",
    )
    st.title("Supabase pgvector workspace")
    st.caption("Direct PostgreSQL access to the private `rag` schema · no Qdrant or SQLite")
    st.session_state.setdefault("history", [])
    st.session_state.setdefault("preview", None)

    try:
        with db_connection() as connection:
            names = list_collections(connection)
    except Exception as error:
        st.error(f"Supabase connection failed: {error}")
        st.info("Set SUPABASE_DATABASE_URL in the RAG project's private .env file.")
        st.stop()

    with st.sidebar:
        st.header("Supabase collections")
        chosen = st.selectbox("Active collection", names, index=0 if names else None)
        if st.button("Refresh collections", icon=":material/refresh:"):
            st.rerun()
        st.divider()
        st.code("SUPABASE_DATABASE_URL", language="text")
        st.caption("Credentials remain server-side in .env.")

    if not chosen:
        st.warning("No collections found in rag.collections.")
        st.stop()

    try:
        with db_connection() as connection:
            require_collection(connection, chosen)
            docs = list_documents(connection, chosen)
    except Exception as error:
        st.error(str(error))
        st.stop()

    with st.container(horizontal=True):
        st.metric("Committed documents", len(docs))
        st.metric("Stored chunks", sum(item["chunks"] for item in docs))
        st.metric("Vector dimensions", get_settings().vector_dimensions)

    documents_tab, questions_tab = st.tabs(["Documents", "Questions"])
    with documents_tab:
        st.subheader("Upload document")
        upload = st.file_uploader("PDF, TXT, or Markdown", type=["pdf", "txt", "md"])
        data = upload.getvalue() if upload else None
        if upload and st.button("Preview chunks", icon=":material/preview:"):
            try:
                text = parse_upload(upload.name, data)
                digest, chunks = build_chunks(upload.name, text, test_document=False)
                st.session_state.preview = {
                    "filename": upload.name,
                    "text": text,
                    "digest": digest,
                    "chunks": chunks,
                }
            except Exception as error:
                st.error(str(error))

        preview = st.session_state.get("preview")
        if preview:
            st.caption(
                f"{len(preview['chunks'])} chunks · {get_settings().vector_dimensions} dimensions · not yet embedded"
            )
            with st.expander("Preview extracted chunks", expanded=True):
                for index, chunk in enumerate(preview["chunks"][:8], 1):
                    st.markdown(f"**Chunk {index}**")
                    st.write(chunk.text)
            category = st.text_input("Document category", value="platform")
            approved = st.checkbox("I approve embedding this document into Supabase.")
            if st.button("Embed and store", type="primary", disabled=not approved):
                try:
                    with db_connection() as connection:
                        result = insert_document(
                            connection,
                            preview["filename"],
                            category,
                            preview["text"],
                            collection_id=chosen,
                            test_document=False,
                        )
                    st.session_state.preview = None
                    st.success(f"Stored {result['chunks']} chunks in {chosen}.")
                    st.rerun()
                except Exception as error:
                    st.error(str(error))

        st.divider()
        st.subheader("Committed documents")
        if docs:
            for document in docs:
                st.write(
                    f"{document['filename']} · {document['chunks']} chunks · {document['category']}"
                )
                st.caption(str(document["id"]))
        else:
            st.info("No committed documents in this collection.")

    with questions_tab:
        st.subheader("Questions")
        mode = st.segmented_control("Response", ["Answer", "Search"], default="Answer") or "Answer"
        doc_names = {item["id"]: item["filename"] for item in docs}
        document_id = st.selectbox(
            "Document scope",
            [None, *doc_names],
            format_func=lambda value: doc_names[value] if value else "All documents",
        )
        if st.button("Clear question history", icon=":material/delete_sweep:"):
            st.session_state.history = []
        for item in st.session_state.history:
            with st.chat_message("user"):
                st.write(item["question"])
            with st.chat_message("assistant"):
                st.write(item["answer"])
                render_sources(item["sources"])

        question = st.chat_input("Ask about the selected collection", disabled=not docs)
        if question:
            try:
                with db_connection() as connection:
                    matches = search_document(connection, question, collection_id=chosen)
                if document_id:
                    matches = [
                        item for item in matches if str(item["document_id"]) == str(document_id)
                    ]
                if mode == "Answer":
                    result, sources = answer_from_matches(question, matches)
                    answer = result["answer"]
                else:
                    answer = f"{len(matches)} matching chunks found."
                    sources = matches
                st.session_state.history.append(
                    {"question": question, "answer": answer, "sources": sources}
                )
                st.session_state.history = st.session_state.history[-20:]
                st.rerun()
            except Exception as error:
                st.error(str(error))


if __name__ == "__main__":
    main()
