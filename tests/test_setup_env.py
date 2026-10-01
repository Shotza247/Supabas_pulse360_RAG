import importlib.util
from pathlib import Path


def module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "setup_env.py"
    spec = importlib.util.spec_from_file_location("setup_env", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_sync_preserves_values_and_archives_previous_config(tmp_path):
    (tmp_path / ".env.example").write_text(
        "HF_TOKEN=\nEMBEDDING_MODEL=BAAI/bge-small-en-v1.5\nVECTOR_DIMENSIONS=384\n"
    )
    old = "HF_TOKEN=hf_test_only\nUNUSED_SETTING=old\nVECTOR_DIMENSIONS=1024\n"
    (tmp_path / ".env").write_text(old)
    module().sync_env(tmp_path)
    from dotenv import dotenv_values

    result = dotenv_values(tmp_path / ".env")
    assert result["HF_TOKEN"] == "hf_test_only"
    assert result["VECTOR_DIMENSIONS"] == "1024"
    assert "UNUSED_SETTING" not in result
    assert next((tmp_path / ".local/archive").glob("*/legacy.env")).read_text() == old


def test_explicit_embedding_configuration_is_preserved(tmp_path):
    (tmp_path / ".env.example").write_text("EMBEDDING_MODEL=default\nVECTOR_DIMENSIONS=384\n")
    (tmp_path / ".env").write_text("EMBEDDING_MODEL=custom\nVECTOR_DIMENSIONS=1024\n")
    module().sync_env(tmp_path)
    from dotenv import dotenv_values

    assert dotenv_values(tmp_path / ".env")["VECTOR_DIMENSIONS"] == "1024"
