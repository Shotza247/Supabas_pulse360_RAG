from faq_agent.config import get_settings


def test_env_values_are_coerced_to_field_types(monkeypatch):
    """Integer settings read from the environment must not stay strings.

    Regression: _COERCE only had string keys, so with no `from __future__ import
    annotations` every value fell back to str and Settings validation raised a
    TypeError as soon as CHUNK_OVERLAP / CONTEXT_K etc. were set in .env or Vercel.
    """
    monkeypatch.setenv("CHUNK_CHARS", "900")
    monkeypatch.setenv("CHUNK_OVERLAP", "100")
    monkeypatch.setenv("CONTEXT_K", "6")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert (settings.chunk_chars, settings.chunk_overlap, settings.context_k) == (900, 100, 6)
    finally:
        get_settings.cache_clear()
