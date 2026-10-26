import pytest

from faq_agent.chunking.chunker import SECTION_RE, _window_text, chunk_document
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


# ── Fix #9: SECTION_RE — all-caps content without colon must NOT be a heading ──

@pytest.mark.parametrize("line", [
    # Markdown headings — must match
    "# Introduction",
    "## Returns Policy",
    "### FAQ Items",
    # All-caps explicit label ending with colon — must match
    "RETURNS POLICY:",
    "OVERVIEW:",
    "DATA & PRIVACY:",
])
def test_section_re_matches_valid_headings(line):
    assert SECTION_RE.match(line), f"Expected match: {line!r}"


@pytest.mark.parametrize("line", [
    # All-caps WITHOUT colon — must NOT match (fix #9)
    "N/A",
    "URL",
    "FAQ",
    "IMPORTANT",
    "NOTE",
    "TRUE",
    "HTTP",
    # Mixed case without markdown marker — must NOT match
    "Some normal sentence",
    "Returns Policy",   # title-case, no colon
])
def test_section_re_rejects_false_positives(line):
    assert not SECTION_RE.match(line), f"Expected no match: {line!r}"


def test_allcaps_content_not_split_into_sections():
    """A document with all-caps lines (but no colon) must produce one section, not many."""
    doc = SourceDocument(
        source_id="test",
        path="test.txt",
        title="Test",
        text="Introduction\nIMPORTANT\nThis is some content.\nNOTE\nMore content here.",
    )
    chunks = chunk_document(doc, max_chars=200, overlap_chars=20)
    sections = {c.metadata["section"] for c in chunks}
    # Should be one section (the default), not split on IMPORTANT/NOTE
    assert len(sections) == 1


def test_allcaps_label_with_colon_splits_sections():
    """A document with ALLCAPS: labels must be split into distinct sections."""
    doc = SourceDocument(
        source_id="test",
        path="test.txt",
        title="Test",
        text="OVERVIEW:\nThis describes the overview.\n\nRETURNS POLICY:\nItems within 14 days.",
    )
    chunks = chunk_document(doc, max_chars=200, overlap_chars=20)
    sections = {c.metadata["section"] for c in chunks}
    assert "OVERVIEW" in sections
    assert "RETURNS POLICY" in sections


# ── Fix #10: _window_text — window must not stall at 1-char advance ──

def test_window_terminates_with_large_overlap():
    """overlap_chars just below max_chars must not produce an infinite or very large window list."""
    text = "x" * 500
    # overlap = max_chars - 1 is the extreme edge case
    windows = _window_text(text, max_chars=100, overlap_chars=99)
    # With min_advance = max(1, 99//2) = 49, we expect ~500/49 ≈ 11 windows, not 500
    assert len(windows) < 20, f"Too many windows ({len(windows)}) — advance guard not working"


def test_window_advance_is_bounded():
    """Each window's start must advance by at least overlap_chars // 2 from the previous."""
    text = "a b c d e f g h " * 50   # 800 chars, spaces every 2
    max_chars = 50
    overlap_chars = 40
    windows = _window_text(text, max_chars=max_chars, overlap_chars=overlap_chars)
    # There must be more than one window (overlap is substantial)
    assert len(windows) > 1
    # Total count must be reasonable — certainly not hundreds
    min_advance = max(1, overlap_chars // 2)  # = 20
    upper_bound = len(text) // min_advance + 2
    assert len(windows) <= upper_bound, f"Too many windows: {len(windows)} > {upper_bound}"


def test_window_overlap_present():
    """Consecutive windows should share some text when overlap_chars > 0."""
    text = "word " * 100  # 500 chars
    windows = _window_text(text, max_chars=60, overlap_chars=20)
    assert len(windows) >= 2
    # The end of window[0] should appear somewhere near the start of window[1]
    overlap_sample = windows[0][-15:]
    assert overlap_sample in windows[1], "Expected overlapping content between consecutive windows"
