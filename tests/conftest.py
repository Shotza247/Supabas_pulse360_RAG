"""Shared pytest fixtures for the faq_agent test suite."""

import pytest

from faq_agent.api.routes import _rate_counters


@pytest.fixture(autouse=True)
def clear_rate_counters():
    """Reset the in-process rate-limit counter before every test.

    The counter is module-level state so it bleeds across tests that share the
    same pytest process, causing spurious 429 failures on routes that happen to
    run after test_rate_limit_returns_429 has saturated the window.
    """
    _rate_counters.clear()
    yield
    _rate_counters.clear()
