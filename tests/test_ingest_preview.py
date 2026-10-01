import importlib.util
import json
from pathlib import Path

from faq_agent.config import Settings


def test_preview_does_not_create_provider_or_store(tmp_path, monkeypatch, capsys):
    script = Path(__file__).resolve().parents[1] / "scripts" / "ingest_faq.py"
    spec = importlib.util.spec_from_file_location("ingest_preview", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "faq.txt").write_text("# Skills\nPython and SQL.")
    monkeypatch.setattr(
        "sys.argv", ["ingest", "--source", str(tmp_path), "--version", "preview", "--dry-run"]
    )
    monkeypatch.setattr(module, "get_settings", lambda: Settings())

    def forbidden(*args, **kwargs):
        raise AssertionError("Dry run must not construct a provider or database")

    monkeypatch.setattr(module, "build_embedding_client", forbidden)
    monkeypatch.setattr(module, "build_vector_store", forbidden)
    module.main()
    result = json.loads(capsys.readouterr().out)
    assert result["documents"] == 1 and result["chunks"] == 1
    assert result["model_calls"] == result["database_writes"] == 0
