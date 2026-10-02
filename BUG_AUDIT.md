# Copy verification and recovery

## 2026-10-02 - Supabase smoke test: NOT NULL column-name mismatches

- Status: fixed in `supabase_pgvector_test.py` (`insert_document`). Re-embedding a
  PDF into `test_collection` now needs no further schema workarounds for these two columns.
- Root cause: `insert_document` builds its INSERT from a dict of candidate values and keeps
  only keys that exist in the live table (`if column in document_columns` /
  `chunk_columns`). A NOT NULL column whose name is not a key in the dict is silently
  skipped, so the failure only appears at the database. The live `rag` schema uses
  different names from the ones the code assumed.
- Fix 1 - `rag.documents.sha256`
  - Error: `null value in column "sha256" of relation "documents" violates not-null constraint`.
  - The code set `checksum` and `content_hash`, but the table column is `sha256`.
  - Added `"sha256": digest` to `document_values`. `digest` is the SHA-256 of the
    extracted text; use `hashlib.sha256(upload_bytes).hexdigest()` instead if the
    schema should hash the raw file.
- Fix 2 - `rag.document_chunks.ordinal`
  - Error: `null value in column "ordinal" of relation "document_chunks" violates not-null constraint`.
  - The code set `chunk_index`, but the table column is `ordinal`.
  - Added `"ordinal": index` to `chunk_values`.
- Why `checksum`, `content_hash` and `chunk_index` stay: the column filter drops them
  automatically when the table lacks them, and they remain valid for schemas that use them.
- Rolled-back inserts: failed attempts do not commit, so no partial rows should remain.
  Confirm with `select * from rag.documents where collection_id = 'test_collection';`.
- Not yet verified: retrieval through `rag.match_document_chunks` after a successful embed,
  and the same inserts from `streamlit_app.py` (it reuses `insert_document`, so it should
  inherit the fix).

## 2026-09-20 - Independent baseline verification

- Status: passed. This validates the inherited Qdrant/SQLite baseline, NOT Supabase.
- 54 allowlisted baseline files copied and SHA-256 matched against the original.
- All 55 offline tests passed from the new project directory, including Streamlit
  AppTest, API, library, chunking, embeddings, retrieval and synthesis tests.
- Used the original project's installed Python dependencies with this project's
  source/test paths. No independent virtual environment installation was tested.
- Command: `..\OpenSource-RAG-Template\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .local\verification-20260920-second`.
- Three warnings: upstream Starlette/httpx and AnyIO deprecations, plus the expected
  in-memory Qdrant warning that payload indexes are ineffective in local mode.
- Initial run: 46 passed and 9 fixture errors because the requested .local parent
  directory did not exist (WinError 3). Created that test-output directory and used
  a new basetemp path; the full rerun passed. No application-code fix was needed.
- Secrets/private data were excluded from the copy. Tests generated only synthetic
  fixtures under the ignored .local test directory.
- No servers started, original configuration changed, cloud writes, model calls,
  commits or Supabase resources created during the split.
- Next: open HANDOFF.md in a separate chat and design the Supabase implementation.