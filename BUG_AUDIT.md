# Copy verification and recovery

## Code-review critical fixes — race condition, context manager, timing oracle (commit 847d8d1)

Three critical findings from the second code review were fixed individually and
committed as a single unit in `847d8d1`.

### Fix R1 — Per-connection VectorDumper (was: shared class-level `.oid` mutation)

- **Root cause**: `_build_vector_dumper()` returned a single `_VectorDumper` class
  stored at module level.  `_register_vector_dumper()` mutated its `oid` class
  attribute in place.  Under concurrent requests two threads could overwrite each
  other's OID assignment — a classic shared-mutable-state race condition.
- **Fix**: Removed the module-level factory and singleton.  `_register_vector_dumper()`
  now creates a **fresh subclass per connection** with `oid` set as a class attribute
  on that subclass only, so concurrent connections never share mutable state.
- **Files changed**: `src/faq_agent/supabase_store.py`

### Fix R2 — `db_connection()` proper context manager (was: bare `psycopg.connect()`)

- **Root cause**: `db_connection()` in `supabase_pgvector_test.py` returned the
  result of `psycopg.connect()` directly.  Callers used it as `with db_connection()
  as connection:`, relying on psycopg3's `Connection.__enter__` implicitly.  If an
  exception was raised inside the `with` block before the connection's `__exit__`
  could run, the connection could be left open.
- **Fix**: Added `@contextmanager` decorator.  The function now uses
  `with psycopg.connect(database_url) as connection: yield connection` — guaranteed
  resource cleanup on any exit path, consistent with `SupabaseLibrary.connection()`.
- **Files changed**: `supabase_pgvector_test.py`

### Fix R3 — Collection-scoped preview SQL query (was: timing oracle)

- **Root cause**: `_load_preview()` fetched the preview row by `id` alone, then
  checked `col_id != collection_id` in Python and raised 410.  An attacker who
  knew a valid `preview_id` from collection A could probe whether it existed in
  collection B: a matching row caused a Python-level check (faster) while a missing
  row returned from the DB directly (slower), creating a timing side-channel.
- **Fix**: Changed the SQL query to `WHERE id = %s AND collection_id = %s`.  No
  Python-side membership check needed; both "not found" and "wrong collection" follow
  the identical code path with no distinguishable timing.
- **Files changed**: `src/faq_agent/supabase_store.py`

---

## Code-review medium fixes — vector parameterization, duplicate check, LLM evidence stripping

### Fix #7 — Remove `vector_literal()` from smoke-test (commit `f52a8b8`)

- **Root cause**: `supabase_pgvector_test.py` built raw SQL strings like
  `"[0.1,0.2,…]"` via `vector_literal()` and passed them as `%s` parameters.
  The production `SupabaseLibrary` path was already fixed in `9fe6ece` by
  registering the psycopg3 type adapter, but the smoke-test was left behind.
- **Fix**: Deleted `vector_literal()` entirely.  `db_connection()` now calls
  `_register_vector_dumper(connection)` (imported from `supabase_store`) so raw
  `list[float]` values pass through psycopg3's normal parameterisation path at
  both call sites (`insert_document` embedding insert and `search_document` query
  vector).  No raw string interpolation of vectors remains anywhere in the codebase.
- **Files changed**: `supabase_pgvector_test.py`

### Fix N1 — `sha256`-first duplicate check in `commit()` (commit `8d47aec`)

- **Root cause**: The duplicate-check loop in `commit()` tried `checksum` first,
  then `content_hash`.  The live Supabase schema uses the column name `sha256`.
  Re-uploading the same document to the live schema was never detected as a
  duplicate and always incurred an unnecessary HuggingFace embedding API call,
  potentially creating duplicate document rows.
- **Fix**: Replaced the two-branch `if/elif` with a priority-ordered loop over
  `("sha256", "checksum", "content_hash")`.  The first column present in the live
  schema is used for the lookup; the loop then breaks.  All three known schema
  variants are covered with no behaviour change for schemas using the older names.
- **Files changed**: `src/faq_agent/supabase_store.py`

### Fix N1-b — `sha256` missing from INSERT `doc_values` (commit `700a264`)

- **Root cause**: The N1 fix corrected the *read* side (duplicate-check SELECT)
  but the *write* side (`doc_values` dict driving the INSERT for new documents)
  still only supplied `checksum` and `content_hash`.  On the live schema, where
  `sha256` is NOT NULL, this caused a constraint violation on every first-time
  upload — even though re-uploads were now correctly detected as duplicates.
  Discovered during a full code audit (2026-10-24).
- **Fix**: Added `"sha256": preview["digest"]` to `doc_values` alongside
  `checksum` and `content_hash`.  The existing column-filter (`doc_cols`) ensures
  only the column present in the live schema is written.  Also added `sha256` to
  `doc_optional` so `commit_schema_mismatch` warnings correctly track it on
  schemas that use the older names.
- **Files changed**: `src/faq_agent/supabase_store.py`

### Fix N2 — Strip internal metadata from LLM evidence payload (commit `ce61583`)

- **Root cause**: `synthesize()` serialised the full match dict — including
  `vector_score`, `score`, `rerank_score`, `document_id`, `filename` and `page`
  — as the LLM user message.  This inflated token usage on every `/ask` call,
  sent internal storage identifiers to a third-party API, and contradicted the
  system prompt's "untrusted data" framing.
- **Fix**: Added `_LLM_EVIDENCE_FIELDS = ("chunk_id", "text", "title", "section")`.
  `synthesize()` builds a stripped `evidence` list from those four fields before
  serialising the prompt.  `validate_answer()` still receives the original full
  `matches` list so API response citations carry complete metadata (scores,
  filename, page) for the caller.  Test updated to assert the stripped payload.
- **Files changed**: `src/faq_agent/llm/client.py`, `tests/test_basic_rag.py`

---

## Critical fixes — production hardening (Critical #1 / #2 / #3)

Three architectural issues in `supabase_store.py` were identified in the code
review and fixed as a single unit.  All three relate to the upload lifecycle
(`preview()` → `commit()`).

### Fix C1 — Durable preview storage (was: in-process `_PREVIEWS` dict)

- **Root cause**: Preview sessions were held in a module-level Python dict
  (`_PREVIEWS`).  A server restart, gunicorn worker recycle, or Vercel cold
  start silently evicted all pending previews, causing unexplained 410 errors
  on the client side with no log evidence.
- **Fix**: Removed `_PREVIEWS` entirely.  Previews are now persisted in a new
  `rag.previews` table in Supabase.  The table is created idempotently on first
  use (`CREATE TABLE IF NOT EXISTS`) and a dedicated migration script is
  provided at `docs/migrations/001_add_rag_previews.sql`.
- **TTL cleanup**: `_expire_previews()` runs a `DELETE WHERE created_at < now()
  - interval '1 hour'` at the start of every `preview()` call, keeping the
  table small without a background job.
- **Committed flag**: After a successful `commit()` transaction the preview row
  is marked `committed = true`, providing an audit trail of what was processed.

### Fix C2 — Duplicate check before embedding API call

- **Root cause**: `commit()` called the paid HuggingFace embedding API before
  checking whether the document was already stored.  If the `checksum` column
  was absent from the live schema the check was skipped entirely, meaning every
  re-upload of the same file incurred an unnecessary API cost.
- **Fix**: The duplicate check now runs first, before any call to
  `build_embedding_client().embed_documents()`.  The guard checks `checksum`
  first and falls back to `content_hash` so it is robust to both schema
  variants.  Embedding is only called when the document is confirmed new.
- **Log**: Skipped duplicates are recorded at `INFO` level:
  `commit_skipped_duplicate collection=… digest=… document_id=…`.

### Fix C3 — Explicit transaction for document + chunk inserts

- **Root cause**: The document INSERT and the chunk loop ran without an
  explicit transaction boundary.  psycopg3 wraps statements in an implicit
  transaction, but a mid-loop failure produced no structured error log and left
  callers with no indication of whether partial data had been written.
- **Fix**: All inserts (document row + every chunk row + preview `committed`
  flag update) now run inside `with connection.transaction():`.  The entire
  block commits atomically or rolls back completely.  On failure:
  - The error is logged at `ERROR` level with
    `commit_transaction_failed collection=… digest=… document_id=… error_type=… error=…`
  - The client receives HTTP 500 with a clear message: "the transaction was
    rolled back — no partial data was written — you may retry by re-submitting
    the same preview_id".
  - Because the preview row is not marked `committed` until the transaction
    succeeds, retrying with the same `preview_id` works cleanly.

**Files changed**: `src/faq_agent/supabase_store.py`, `docs/architecture.md`
**Migration required**: `docs/migrations/001_add_rag_previews.sql`

---

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