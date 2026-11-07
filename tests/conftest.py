"""Shared pytest fixtures for the faq_agent test suite."""

import pytest

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
