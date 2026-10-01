from pathlib import Path
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "streamlit_app.py"


def test_create_select_search_and_answer():
    collections = []
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "POST" and url.endswith("/collections"):
            collections.append(
                {
                    "id": kwargs["json"]["name"],
                    "physical_name": "test-index",
                    "compatible": True,
                    "dimensions": 384,
                    "embedding_model": "test/model",
                }
            )
            data = collections[-1]
        elif url.endswith("/documents"):
            data = [{"id": "a" * 64, "filename": "faq.md", "chunks": 2}]
        elif url.endswith("/ask"):
            data = {"answer": "An example answer.", "sources": [], "request_id": "test-ask"}
        elif url.endswith("/search"):
            data = {"matches": [], "request_id": "test-search"}
        else:
            data = collections.copy()
        return Mock(ok=True, json=lambda: data)

    with patch("requests.request", side_effect=request):
        at = AppTest.from_file(str(APP), default_timeout=20).run()
        assert not at.exception
        at.text_input[0].set_value("pulse360_faq")
        next(b for b in at.button if b.label == "Create collection").click().run()
        assert not at.exception
        assert at.selectbox(key="collection").value == "pulse360_faq"
        at.chat_input[0].set_value("What is the FAQ?").run()
        assert not at.exception
        assert any("An example answer." in t.value for t in at.text)
        at.segmented_control[0].set_value("Search").run()
        at.chat_input[0].set_value("Find the FAQ").run()
        assert not at.exception
        assert calls[-2][2].get("json", {}).get("collection_id") == "pulse360_faq" or any(
            c[1].endswith("/search") and c[2]["json"]["collection_id"] == "pulse360_faq"
            for c in calls
        )


def test_api_down_is_visible():
    import requests

    with patch("requests.request", side_effect=requests.ConnectionError):
        at = AppTest.from_file(str(APP), default_timeout=20).run()
        assert not at.exception
        assert "could not be reached" in at.error[0].value
