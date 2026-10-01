from faq_agent.chunking.chunker import chunk_document
from faq_agent.schemas import SourceDocument


def test_chunk_document_keeps_section_metadata() -> None:
    doc = SourceDocument(
        source_id="faq",
        path="faq.md",
        title="FAQ",
        text="# Experience\nBuilt AI systems.\n\n# Projects\nCreated Pulse360 and Fleet360.",
    )

    chunks = chunk_document(doc, max_chars=80, overlap_chars=10)

    assert chunks
    assert {chunk.metadata["section"] for chunk in chunks} == {"Experience", "Projects"}
    assert all(chunk.chunk_id for chunk in chunks)
