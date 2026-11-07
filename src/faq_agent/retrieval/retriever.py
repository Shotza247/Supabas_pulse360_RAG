"""Shared retrieval and answer workflow, independent of HTTP routes."""

from functools import lru_cache

from faq_agent.config import get_settings
from faq_agent.llm.client import synthesize
from faq_agent.supabase_store import SupabaseLibrary


@lru_cache(maxsize=1)
def _library(settings) -> SupabaseLibrary:
    """Return a single shared SupabaseLibrary instance.

    Settings is a frozen dataclass so it is hashable and safe to use as a cache
    key.  Creating a new SupabaseLibrary on every request opened a new psycopg3
    connection each time, burning through the Supabase connection limit under
    any meaningful load (finding #14).
    """
    return SupabaseLibrary(settings)


def answer_question(question, collection_id=None, document_id=None):
    settings = get_settings()
    matches = search_question(question, collection_id, document_id)
    return synthesize(question, matches, settings)


def search_question(question, collection_id=None, document_id=None):
    settings = get_settings()
    if not collection_id:
        collection_id = settings.collection
    return _library(settings).search(collection_id, question, document_id)
