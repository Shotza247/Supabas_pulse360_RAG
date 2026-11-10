"""Shared pytest fixtures for the faq_agent test suite."""

import pytest

from faq_agent.config import get_settings
from faq_agent.rate_limiter import clear_local


@pytest.fixture(autouse=True)
def clear_rate_counters():
    """Reset the in-process rate-limit counter before every test.

    The counter lives in rate_limiter._local_counters.  The alias
    _rate_counters in routes.py points to the same object so both modules
    stay in sync.  Tests run without a live DB so the local fallback path
    is always used.
    """
    clear_local()
    yield
    clear_local()

# Credentials, connections and provider switches that must never leak from a developer's real .env into the
# offline suite. They are set to "" rather than deleted: get_settings() calls
# load_dotenv(), which only fills variables that are missing from the environment.
_ISOLATED_ENV = (
    "SUPABASE_DATABASE_URL",
    "DATABASE_URL",
    "HF_TOKEN",
    "OPENAI_API_KEY",
    "EMBEDDING_PROVIDER",
    "LLM_PROVIDER",
    "QUERY_API_KEY",
    "ADMIN_API_KEY",
    "PUBLIC_COLLECTIONS",
)


@pytest.fixture(autouse=True)
def isolate_from_dotenv(request, monkeypatch):
    """Keep offline tests off the real database and model APIs, whatever .env holds.

    With production values filled in, tests that reach the rate limiter or an embedding
    client opened real network connections and the suite stalled for minutes.
    Live tests opt in explicitly (see tests/test_live_openai_pgvector.py).
    """
    if request.node.get_closest_marker("live"):
        get_settings.cache_clear()  # live tests read the real .env on purpose
        yield
        get_settings.cache_clear()
        return
    for name in _ISOLATED_ENV:
        monkeypatch.setenv(name, "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
