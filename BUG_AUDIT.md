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
- Retrieval through `rag.match_document_chunks` is covered by the offline test suite
  (70 passing tests).  `streamlit_app.py` now imports from `faq_agent.db_helpers`
  (fix N5, commit `4961787`) which uses the same `insert_document` path with the same
  `sha256`/`ordinal` fixes — no separate verification needed.

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

---

## Session 3 fixes — chunking, rate limiting, config, structure (commits b5e39fa–fa6796c)

### Fix #9 — SECTION_RE colon requirement (commit `b5e39fa`)

- **Root cause**: The all-caps branch matched any sequence of 4+ uppercase letters,
  digits and punctuation regardless of a trailing colon.  Words like `IMPORTANT`,
  `NOTE`, `FAQ`, `URL`, `N/A` and table headers were being treated as section
  boundaries, fragmenting text into spurious single-line sections and degrading
  retrieval quality.
- **Fix**: Changed `:?` (optional colon) to `:$` (required colon with end-of-line anchor)
  on the all-caps branch.  `RETURNS POLICY:` still splits; `IMPORTANT` does not.
- **Tests added**: `test_section_re_matches_valid_headings`, `test_section_re_rejects_false_positives`,
  `test_allcaps_content_not_split_into_sections`, `test_allcaps_label_with_colon_splits_sections`.
- **Files changed**: `src/faq_agent/chunking/chunker.py`, `tests/test_chunking.py`

### Fix #10 — `_window_text` minimum advance guard (commit `b5e39fa`)

- **Root cause**: `start = max(start + 1, end - overlap_chars)` stalled at +1 per
  iteration when `overlap_chars` was close to `max_chars`.  On a 100 000-character
  section with `overlap=999` and `max_chars=1000` this produced ~100 000 windows,
  hanging ingestion indefinitely.
- **Fix**: `min_advance = max(1, overlap_chars // 2)` computed once; advance guard
  becomes `start = max(start + min_advance, end - overlap_chars)`.
- **Tests added**: `test_window_terminates_with_large_overlap`, `test_window_advance_is_bounded`,
  `test_window_overlap_present`.
- **Files changed**: `src/faq_agent/chunking/chunker.py`, `tests/test_chunking.py`

### Fix #13 — Rate limiting on /search and /ask (commit `5e83ebf`)

- **Root cause**: Both API endpoints had no throttle on how many requests a single
  IP could send per second, exposing the HuggingFace API quota to exhaustion from
  any motivated client.
- **Fix**: Added an in-process fixed-window counter (`defaultdict`).  Limit: 30
  requests per IP per 60-second window.  HTTP 429 is returned before any embedding
  or DB work begins.  No external dependency added.  Counter resets between test
  runs via an autouse pytest fixture in `tests/conftest.py`.
- **Caveat**: In-process only — not shared across multiple workers.  A Redis/Supabase
  counter is needed for multi-worker public deployment.
- **Files changed**: `src/faq_agent/api/routes.py`, `tests/conftest.py`, `tests/test_api.py`

### Fix #14 — SupabaseLibrary singleton (commit `3941ccf`)

- **Root cause**: `search_question()` in `retriever.py` called `SupabaseLibrary(settings)`
  on every request, opening a new psycopg3 connection and then discarding it.  Under
  load this drained the Supabase connection limit quickly.
- **Fix**: Added `@lru_cache(maxsize=1)` on a `_library(settings)` factory.  Since
  `Settings` is a frozen dataclass it is hashable and safe as a cache key.  A single
  `SupabaseLibrary` instance is reused for the process lifetime.
- **Files changed**: `src/faq_agent/retrieval/retriever.py`, `tests/test_basic_rag.py`

### Fix N3 — Settings hard crash for non-HuggingFace provider (commit `829a158`)

- **Root cause**: `Settings.__post_init__` raised `ValueError` for any `llm_provider`
  value other than `"huggingface"`.  Embedding-only or retrieval-only deployments
  that set `llm_provider=openai` (for future use) crashed at config load time, before
  any synthesis code was reached.
- **Fix**: Removed the `__post_init__` guard entirely.  `synthesize()` in
  `llm/client.py` already raises a clear `ValueError` at the point of use for
  unsupported providers.
- **Files changed**: `src/faq_agent/config.py`, `tests/test_api.py`

### Fix N4 — Default section name "FAQ" renamed to "General" (commit `138e90b`)

- **Root cause**: Documents without any markdown heading or `LABEL:` line produced
  chunks with `section="FAQ"`.  This label was misleading for non-FAQ content (e.g.
  product manuals, policies), and appeared as a filter option in the Streamlit UI
  even for documents that contained no FAQ structure.
- **Fix**: Renamed the default `current_name` from `"FAQ"` to `"General"` in
  `_split_sections()`.  Also updated the fallback in the header-strip expression.
- **Files changed**: `src/faq_agent/chunking/chunker.py`, `tests/test_chunking.py`,
  `tests/test_faq_context.py`

### Fix N5 — streamlit_app.py imported from smoke-test script (commit `4961787`)

- **Root cause**: `streamlit_app.py` imported `build_chunks`, `db_connection`,
  `insert_document`, `list_collections`, `parse_upload`, `require_collection` and
  `search_document` directly from `supabase_pgvector_test.py`.  Any change to the
  smoke-test (a dev tool) could silently break the production workspace UI with no
  test coverage.
- **Fix**: Extracted all shared helpers into `src/faq_agent/db_helpers.py` (a proper
  package module with tests via the existing imports).  Both `streamlit_app.py` and
  `supabase_pgvector_test.py` now import from `faq_agent.db_helpers`.  The smoke-test
  is reduced to `main()` plus its own constants.
- **Files changed**: `src/faq_agent/db_helpers.py` (new), `streamlit_app.py`,
  `supabase_pgvector_test.py`

### Fix #15 — source_id collision across directories (commit `fa6796c`)

- **Root cause**: `load_document()` set `source_id = path.stem.lower().replace(" ", "-")`.
  Two files named `faq.txt` in different directories produced the same `source_id`,
  causing chunk deduplication and retrieval mismatches in any corpus with repeated
  filenames across sub-directories.
- **Fix**: `source_id = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]`.
  The resolved absolute path is always unique.  This is now consistent with the
  `load_documents()` batch path which already used sha256.
- **Files changed**: `src/faq_agent/ingestion/loader.py`

### Fix #16 — Redundant `score` alias alongside `vector_score` (commit `78717f2`)

- **Root cause**: `SupabaseLibrary.search()` returned every match with both
  `"vector_score": similarity` and `"score": similarity`.  `SearchHit` in
  `routes.py` had a separate `score: float | None` field in addition to the
  inherited `vector_score`.  Two fields for the same value confused callers and
  the OpenAPI schema.
- **Fix**: Removed `"score"` from the search result dict in `supabase_store.py`
  and removed the `score` field from `SearchHit` in `routes.py`.  Only
  `vector_score` remains.
- **Files changed**: `src/faq_agent/supabase_store.py`, `src/faq_agent/api/routes.py`,
  `tests/test_search.py`, `tests/test_basic_rag.py`

