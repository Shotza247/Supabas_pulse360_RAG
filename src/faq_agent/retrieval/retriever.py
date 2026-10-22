"""Shared retrieval and answer workflow, independent of HTTP routes."""

from faq_agent.config import get_settings
from faq_agent.llm.client import synthesize
from faq_agent.supabase_store import SupabaseLibrary


def answer_question(question, collection_id=None, document_id=None):
    settings = get_settings()
    matches = search_question(question, collection_id, document_id)
    return synthesize(question, matches, settings)


def search_question(question, collection_id=None, document_id=None):
    settings = get_settings()
    if not collection_id:
        collection_id = settings.collection
    return SupabaseLibrary(settings).search(collection_id, question, document_id)
