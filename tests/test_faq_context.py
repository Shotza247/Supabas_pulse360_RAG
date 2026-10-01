from faq_agent.api.routes import create_app
from faq_agent.chunking.chunker import chunk_document
from faq_agent.prompts.templates import ABSTENTION, SYSTEM_PROMPT
from faq_agent.schemas import SourceDocument


def test_faq_identity_and_defaults():
    assert create_app().title == "FAQ Document RAG"
    assert "FAQ" in ABSTENTION
    assert "FAQ evidence" in SYSTEM_PROMPT
    chunks = chunk_document(SourceDocument("faq", "sample.txt", "FAQ", "Contact support."))
    assert chunks[0].metadata["section"] == "FAQ"
