import importlib.util
from pathlib import Path

import pytest


def test_recall_and_reciprocal_rank():
    path = Path(__file__).resolve().parents[1] / "scripts/evaluate_retrieval.py"
    spec = importlib.util.spec_from_file_location("evaluate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.score_results(["a", "b"], ["x", "a"]) == (0.5, 0.5)
    assert module.score_results(["a"], ["x"]) == (0.0, 0)
    with pytest.raises(ValueError):
        module.score_results([], [])
