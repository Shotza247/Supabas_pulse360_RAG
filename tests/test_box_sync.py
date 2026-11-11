import io

import pytest
from docx import Document
from fastapi import HTTPException

from faq_agent.config import Settings
from faq_agent.ingestion.loader import docx_text
from faq_agent.integrations.box_source import (
    BoxClient,
    BoxError,
    BoxFileTooLarge,
    missing_box_settings,
)
from faq_agent.integrations.box_sync import sync_box
from faq_agent.supabase_store import SupabaseLibrary

# ----------------------------------------------------------------------- docx


def make_docx(paragraphs=(), table=None) -> bytes:
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    if table:
        grid = doc.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                grid.cell(r, c).text = value
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def test_docx_text_reads_paragraphs_and_tables_in_order():
    data = make_docx(["Refund policy"], table=[["Plan", "Days"], ["Pro", "30"]])
    text = docx_text(data)
    assert "Refund policy" in text
    assert "Plan | Days" in text and "Pro | 30" in text


def test_docx_rejects_decompression_bomb(monkeypatch):
    monkeypatch.setattr("faq_agent.ingestion.loader.MAX_DOCX_UNCOMPRESSED", 10)
    with pytest.raises(ValueError, match="expands"):
        docx_text(make_docx(["x" * 100]))


def test_preview_accepts_docx_and_rejects_unknown_types(monkeypatch):
    library = object.__new__(SupabaseLibrary)
    library.s = Settings()
    monkeypatch.setattr(SupabaseLibrary, "require_collection", lambda self, c: None)
    with pytest.raises(HTTPException) as exc:
        library.preview("c1", "notes.xlsx", b"data")
    assert exc.value.status_code == 415 and "DOCX" in exc.value.detail
    with pytest.raises(HTTPException) as bad:  # a non-zip payload is reported, not a 500
        library.preview("c1", "broken.docx", b"not a zip")
    assert bad.value.status_code == 422


# ------------------------------------------------------------------ fake Box


class FakeResponse:
    def __init__(self, status=200, body=None, content=b"", headers=None):
        self.status_code, self._body, self._content = status, body, content
        self.headers = headers or {}

    def json(self):
        return self._body

    def iter_content(self, size):
        for i in range(0, len(self._content), size):
            yield self._content[i : i + size]

    def close(self):
        pass


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def settings(**overrides):
    base = {"box_client_id": "id", "box_client_secret": "secret", "box_enterprise_id": "1"}
    return Settings(**{**base, **overrides})


TOKEN = FakeResponse(body={"access_token": "tok", "expires_in": 3600})


def test_missing_settings_names_only_what_is_missing():
    assert missing_box_settings(Settings(), folder_id="9") == [
        "BOX_CLIENT_ID",
        "BOX_CLIENT_SECRET",
        "BOX_ENTERPRISE_ID",
    ]
    assert missing_box_settings(settings()) == ["BOX_FOLDER_ID or BOX_SHARED_LINK"]
    assert missing_box_settings(Settings(box_developer_token="t"), shared_link="u") == []


def test_token_is_fetched_once_and_used_as_bearer():
    session = FakeSession([TOKEN, FakeResponse(body={"entries": []}), FakeResponse(body={"entries": []})])
    box = BoxClient(settings(), session=session)
    list(box.iter_files("1"))
    list(box.iter_files("2"))
    token_calls = [c for c in session.calls if c[1].endswith("/oauth2/token")]
    assert len(token_calls) == 1
    assert token_calls[0][2]["data"]["box_subject_type"] == "enterprise"
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer tok"


def test_token_failure_message_never_contains_credentials():
    box = BoxClient(settings(), session=FakeSession([FakeResponse(status=400)]))
    with pytest.raises(BoxError) as exc:
        box._access_token()
    assert "secret" not in str(exc.value)


def test_iter_files_recurses_pages_and_ignores_other_types():
    session = FakeSession(
        [
            TOKEN,
            FakeResponse(
                body={
                    "entries": [
                        {"type": "file", "id": "10", "name": "a.pdf"},
                        {"type": "folder", "id": "2"},
                        {"type": "web_link", "id": "3"},
                    ],
                    "next_marker": "m1",
                }
            ),
            FakeResponse(body={"entries": [{"type": "file", "id": "11", "name": "b.md"}]}),
            FakeResponse(body={"entries": [{"type": "file", "id": "12", "name": "c.txt"}]}),
        ]
    )
    names = [f["name"] for f in BoxClient(settings(), session=session).iter_files("1")]
    assert names == ["a.pdf", "b.md", "c.txt"]
    assert session.calls[2][2]["params"]["marker"] == "m1"


def test_shared_link_is_sent_as_boxapi_header_and_developer_token_skips_oauth():
    session = FakeSession([FakeResponse(body={"id": "5", "type": "file"})])
    box = BoxClient(Settings(box_developer_token="dev"), session=session)
    assert box.resolve_shared_link("https://app.box.com/s/abc")["id"] == "5"
    headers = session.calls[0][2]["headers"]
    assert headers["BoxApi"] == "shared_link=https://app.box.com/s/abc"
    assert headers["Authorization"] == "Bearer dev"
    assert len(session.calls) == 1


def test_transient_errors_are_retried_then_raised(monkeypatch):
    monkeypatch.setattr("faq_agent.integrations.box_source.time.sleep", lambda _: None)
    ok = FakeSession([TOKEN, FakeResponse(status=429), FakeResponse(body={"entries": []})])
    assert list(BoxClient(settings(), session=ok).iter_files("1")) == []
    bad = FakeSession([TOKEN] + [FakeResponse(status=503)] * 3)
    with pytest.raises(BoxError, match="503"):
        list(BoxClient(settings(), session=bad).iter_files("1"))


def test_expired_token_is_refreshed_once(monkeypatch):
    session = FakeSession(
        [TOKEN, FakeResponse(status=401), TOKEN, FakeResponse(body={"entries": []})]
    )
    assert list(BoxClient(settings(), session=session).iter_files("1")) == []
    assert sum(c[1].endswith("/oauth2/token") for c in session.calls) == 2


def test_download_enforces_size_limit_and_returns_bytes():
    box = BoxClient(
        Settings(box_developer_token="t"),
        session=FakeSession([FakeResponse(content=b"x" * 50), FakeResponse(content=b"abc")]),
        max_bytes=10,
    )
    with pytest.raises(BoxFileTooLarge):
        box.download("1")
    assert box.download("2") == b"abc"


# ---------------------------------------------------------------- sync logic


class FakeLibrary:
    def __init__(self, tracked=None, fail_names=()):
        self.tracked, self.fail_names = tracked or {}, set(fail_names)
        self.committed, self.deleted, self.scope_seen = [], [], None
        self.next_commit_status = "stored"

    def tracked_documents(self, collection_id, source, scope=None):
        self.scope_seen = scope
        return dict(self.tracked)

    def preview(self, collection_id, name, data):
        if name in self.fail_names:
            raise HTTPException(422, "Cannot preview this file")
        return {"preview_id": f"p-{name}"}

    def commit(self, collection_id, preview_id, approved, category="platform", metadata=None):
        self.committed.append((preview_id, approved, metadata))
        return {"status": self.next_commit_status, "document_id": "new", "chunks": 1}

    def delete_document(self, collection_id, document_id):
        self.deleted.append(document_id)
        return True


class FakeBox:
    def __init__(self, files, fail_download=()):
        self.files, self.fail_download = files, set(fail_download)

    def iter_files(self, folder_id, shared_link=None):
        return iter(self.files)

    def download(self, file_id, shared_link=None):
        if file_id in self.fail_download:
            raise BoxError("Box API returned 500 for /files/x/content")
        return b"data"


def entry(file_id, name, sha1="v1", size=100):
    return {"type": "file", "id": file_id, "name": name, "sha1": sha1, "size": size}


FOLDER = {"id": "77", "type": "folder"}


def test_new_file_is_added_with_source_metadata():
    library = FakeLibrary()
    report = sync_box(library, FakeBox([entry("1", "faq.docx")]), "col", FOLDER)
    assert report["added"] == ["faq.docx"]
    metadata = library.committed[0][2]
    assert metadata == {
        "external_source": "box",
        "external_id": "1",
        "external_version": "v1",
        "external_scope": "77",
        "external_name": "faq.docx",
    }
    assert library.scope_seen == "77" and library.committed[0][1] is True


def test_unchanged_file_is_skipped_without_download():
    library = FakeLibrary({"1": {"document_id": "d1", "version": "v1"}})
    box = FakeBox([entry("1", "faq.md")], fail_download={"1"})
    report = sync_box(library, box, "col", FOLDER)
    assert report["unchanged"] == ["faq.md"] and not library.committed and not report["errors"]


def test_changed_file_is_replaced_after_new_version_is_stored():
    library = FakeLibrary({"1": {"document_id": "old", "version": "v1"}})
    report = sync_box(library, FakeBox([entry("1", "faq.md", sha1="v2")]), "col", FOLDER)
    assert report["updated"] == ["faq.md"] and library.deleted == ["old"]
    assert library.committed[0][2]["external_version"] == "v2"


def test_failed_replacement_keeps_the_old_version():
    library = FakeLibrary({"1": {"document_id": "old", "version": "v1"}}, fail_names={"faq.md"})
    report = sync_box(library, FakeBox([entry("1", "faq.md", sha1="v2")]), "col", FOLDER)
    assert library.deleted == [] and report["updated"] == []
    assert report["errors"] == ["faq.md: Cannot preview this file"]


def test_removed_box_file_is_pruned_but_others_are_kept():
    library = FakeLibrary(
        {
            "1": {"document_id": "d1", "version": "v1"},
            "2": {"document_id": "d2", "version": "v1"},
        }
    )
    report = sync_box(library, FakeBox([entry("1", "keep.md")]), "col", FOLDER)
    assert report["removed"] == ["2"] and library.deleted == ["d2"]


def test_empty_listing_refuses_to_prune_unless_allowed():
    tracked = {"1": {"document_id": "d1", "version": "v1"}}
    library = FakeLibrary(tracked)
    report = sync_box(library, FakeBox([]), "col", FOLDER)
    assert library.deleted == [] and "refusing to remove 1" in report["errors"][0]
    allowed = sync_box(library, FakeBox([]), "col", FOLDER, allow_empty=True)
    assert library.deleted == ["d1"] and allowed["removed"] == ["1"]


def test_unsupported_and_oversized_files_are_skipped_not_failed():
    box = FakeBox([entry("1", "sheet.xlsx"), entry("2", "big.pdf", size=50 * 1024 * 1024)])
    report = sync_box(FakeLibrary(), box, "col", FOLDER)
    assert len(report["skipped"]) == 2 and not report["errors"]


def test_one_failing_download_does_not_stop_the_rest():
    box = FakeBox([entry("1", "a.md"), entry("2", "b.md")], fail_download={"1"})
    report = sync_box(FakeLibrary(), box, "col", FOLDER)
    assert report["added"] == ["b.md"] and len(report["errors"]) == 1
    assert "secret" not in report["errors"][0].lower()


def test_dry_run_changes_nothing():
    library = FakeLibrary({"9": {"document_id": "gone", "version": "v1"}})
    report = sync_box(library, FakeBox([entry("1", "a.md")]), "col", FOLDER, dry_run=True)
    assert report["added"] == ["a.md"] and report["removed"] == ["9"]
    assert not library.committed and not library.deleted


def test_single_file_root_does_not_prune_other_files():
    library = FakeLibrary({"5": {"document_id": "d5", "version": "v1"}})
    root = entry("5", "template.docx")
    report = sync_box(library, FakeBox([]), "col", root)
    assert report["unchanged"] == ["template.docx"] and not library.deleted


def test_identical_content_already_stored_is_reported_not_duplicated():
    library = FakeLibrary()
    library.next_commit_status = "already_stored"
    report = sync_box(library, FakeBox([entry("1", "a.md")]), "col", FOLDER)
    assert report["added"] == [] and "already stored" in report["skipped"][0]
