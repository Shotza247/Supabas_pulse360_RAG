"""Pipeline invariants that must survive package and configuration changes."""

from dataclasses import replace
from pathlib import Path

import pytest

from faq_agent.chunking.chunker import chunk_document
from faq_agent.config import PROJECT_ROOT, Settings
from faq_agent.ingestion.loader import load_documents
from faq_agent.ingestion.service import ROOT
from faq_agent.schemas import SourceDocument


@pytest.mark.parametrize("size,overlap", [(0, 0), (5, 5), (5, -1)])
def test_chunk_configuration_rejected(size, overlap):
    with pytest.raises(ValueError):
        chunk_document(SourceDocument("a", "p", "t", "text"), max_chars=size, overlap_chars=overlap)


def test_chunk_bounds_ids_and_source_paths():
    doc = SourceDocument("a", "PRIVATE", "FAQ", "# Product\n" + "dashboard detail " * 100)
    chunks = chunk_document(doc, max_chars=80, overlap_chars=20)
    assert all(0 < len(c.text) <= 80 for c in chunks)
    assert all("source_path" not in c.metadata for c in chunks)
    assert len({c.chunk_id for c in chunks}) == len(chunks)
    assert chunks == chunk_document(doc, max_chars=80, overlap_chars=20)


def test_source_names_do_not_collide(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "faq.txt").write_text("One")
    (tmp_path / "b" / "faq.txt").write_text("Two")
    assert len({d.source_id for d in load_documents(tmp_path)}) == 2


def test_configuration_keeps_existing_index_identity():
    settings = Settings(collection="embedding_demo")
    assert settings.index_name == "embedding_demo-bd228c87eb28"
    assert replace(settings, embedding_model="different").index_name != settings.index_name
    assert replace(settings, embedding_query_prefix="query: ").index_name != settings.index_name
    assert replace(settings, corpus_version="v2").index_name == settings.index_name


def test_catalog_remains_under_project_root():
    assert PROJECT_ROOT == Path(__file__).resolve().parents[1]
    assert ROOT == PROJECT_ROOT / ".local" / "library.sqlite3"
