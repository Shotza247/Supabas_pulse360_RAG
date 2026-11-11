# Architecture

## Responsibilities

The package is `faq_agent`; responsibility folders live inside it to keep imports
unambiguous. HTTP routes validate requests and redact upstream failures. The shared
retrieval module uses `SupabaseLibrary` for all storage operations.
The hosted LLM client generates answers and validates citation IDs against the
retrieved evidence. Prompts are separate from client transport code.

`SupabaseLibrary` (`supabase_store.py`) owns the upload lifecycle: preview, approval
and commit. `ingestion/loader.py` reads local files and extracts `.docx` text
(`docx_text()`, which also serves uploads); PDF extraction in the upload path is
page-aware. All paths use the same chunker and embedding client.
`rate_limiter.py` holds the request limiter and `db_helpers.py` the direct-PostgreSQL
helpers shared by the two Streamlit pages.

```mermaid
sequenceDiagram
    participant U as Streamlit
    participant A as FastAPI
    participant S as Supabase (rag schema)
    participant E as HF embeddings
    participant L as HF LLM
    U->>A: Preview upload in selected collection
    A->>S: Extract and chunk locally, persist preview row
    S-->>U: Preview ID, text, pages and dimensions
    U->>A: Approve preview, embed and store
    A->>S: Load preview (id AND collection_id), duplicate check (sha256/checksum/content_hash)
    Note over A,S: Embedding API is NOT called for duplicates
    A->>E: Approved chunk text (new documents only)
    E-->>A: Validated vectors
    A->>S: BEGIN transaction — insert document + all chunks + mark preview committed
    Note over A,S: Full rollback on any failure; preview_id retryable
    S-->>U: Committed document record
    U->>A: Ask question with collection/document scope
    A->>E: Embed question (per-connection VectorDumper — no shared state)
    A->>S: rag.match_document_chunks similarity search
    S-->>A: Chunks and vector scores
    A->>L: Question + evidence stripped to {chunk_id, text, title, section}
    L-->>A: Answer and citation IDs
    A-->>U: Validated answer with full citation metadata
```

## Data and configuration

- Supabase (`rag` schema): all persistent state — collections, documents, chunk
  embeddings (pgvector), expiring upload previews (`rag.previews`) and rate-limit
  counters (`rag.rate_limits`). Run the files in `docs/migrations/` before deploying.
- `.env`: private runtime settings. `.env.example`: supported keys and defaults.
  `config.py` validates settings at startup and coerces environment values to the
  declared field types (int/float/str). Existing retired keys in a private `.env`
  are ignored. Outside `APP_ENV=local`, `EMBEDDING_URL` and `LLM_BASE_URL` must be HTTPS.
- Embedding provider, model, revision, dimensions, prefixes and chunk configuration
  determine the collection fingerprint. Changing this profile requires a new index.
  The LLM model can change without re-embedding documents.
- Default embedding: `EMBEDDING_PROVIDER=huggingface`, model `BAAI/bge-small-en-v1.5`,
  384 dimensions. `EMBEDDING_PROVIDER=openai` is also supported; vectors are shortened
  to `VECTOR_DIMENSIONS`. The hosted URL must actually serve the configured model.
  Answer model selection is configurable via `LLM_MODEL`; provider access and credits
  must be checked separately.

## Contracts

Both `/search` and `/ask` accept `question`, optional `collection_id`, and optional
`document_id`. Document scope requires collection scope. `/ask` calls the shared
Python retrieval function, not the `/search` HTTP route. Empty retrieval skips the
LLM.

Access control: `/search` and `/ask` require `QUERY_API_KEY` as `X-API-Key`. With the
key unset, only `APP_ENV=local` stays open; any other environment fails closed with
401. A requested `collection_id` must be in `PUBLIC_COLLECTIONS` (default: the
configured `COLLECTION`), otherwise 403. The `/collections` routes use a separate
`ADMIN_API_KEY` and stay locked when it is unset. Responses carry an `X-Request-Id`
header.

The UI only lists application-managed collections. Omitting `collection_id` selects
the configured default collection.

Approval is required before embedding. The duplicate check tries `sha256`, `checksum`, and `content_hash` in that order,
stopping when it finds a matching row. This covers known schema variants and
pre-existing records. The check runs before the
embedding API is called, and the INSERT supplies values for all three column names
(the column filter keeps only the one that exists). Re-uploading a known document
never incurs an embedding cost.

On the Supabase path, previews are persisted in `rag.previews` and survive server
restarts. Preview lookup filters on both `id` AND `collection_id` in a single SQL
query — a token from one collection cannot be used to probe another. A failed
commit leaves no orphaned rows because the document + chunk inserts run inside a
single explicit `connection.transaction()` block. The `preview_id` is retryable:
resubmitting after a transient failure re-embeds and re-writes cleanly.

The pgvector type adapter creates a fresh `Dumper` subclass per connection. The
`oid` is captured as a class attribute on that per-connection subclass so concurrent
connections never share mutable state.

The LLM evidence payload is stripped to `{chunk_id, text, title, section}` before
the API call. Internal IDs, similarity scores, filenames and page numbers are never
sent to the third-party LLM — they remain server-side and are added back to the API
response citations by `validate_answer()` after the LLM call returns.

Only committed documents are searchable.

Uploads accept PDF, DOCX, TXT and Markdown (10 MB). A `.docx` is a zip archive, so
`docx_text()` rejects archives whose uncompressed size exceeds 50 MB before parsing,
and includes table rows in document order.

Rate limiting applies to `/search` and `/ask`: 30 requests per IP per 60-second
window. The counter is stored in `rag.rate_limits` (Supabase) so it is shared
across all Vercel serverless invocations and Gunicorn workers. It falls back to an
in-process counter when no database URL is configured (tests, local dev) or when
the database call fails. The limited IP is the socket peer, unless the caller
authenticated with `QUERY_API_KEY`, in which case a valid `X-Forwarded-Client-IP`
names the real visitor (Pulse360 calls this API from its own server). Requests over
the limit receive HTTP 429 before any embedding or DB work begins.

The shared `SupabaseLibrary` instance is cached via `lru_cache` on the frozen
`Settings` key. Connections are opened per operation; this does not create a
connection pool, so deployment connection limits should be reviewed.

Shared database helpers (connection context manager, document upload/search) live
in `faq_agent.db_helpers` so that `streamlit_app.py` and `supabase_pgvector_test.py`
share a single implementation with no cross-dependency between UI and smoke-test.

`load_document()` derives `source_id` from `sha256(resolved_path)` so files with
the same stem in different directories always get distinct IDs, consistent with the
batch `load_documents()` path.

The default chunk section name is `"General"` for documents that contain no
recognised heading. The section regex requires a colon for all-caps labels
(`RETURNS POLICY:`) to avoid treating acronyms and table headers as boundaries.

## Migrations

| File | Purpose |
|------|---------|
| `docs/migrations/001_add_rag_previews.sql` | Add `rag.previews` table for durable upload session storage |
| `docs/migrations/002_add_rate_limits.sql` | Add `rag.rate_limits` table for distributed cross-worker rate limiting |

## Limitations and next steps

Chunking uses sections and overlapping character windows, not an embedding model.
Default sizes are 1000 characters with 150 overlap; PDF pages are processed
separately. The preview shows planned dimensions, not a computed vector.

Similarity scores are not confidence estimates. Citation validation checks that
IDs exist in the evidence; factual quality needs evaluation. There is no active
reranking, conversational memory, Drive sync or monitoring stack.

Chroma and SQLite code (findings #11, #12) was removed and is not in the
current codebase.

LLM synthesis supports `huggingface` (dev/sandbox) and `openai` (production).
Switch providers by updating `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL`, and
the corresponding API key in `.env`.

All tracked code-review findings are resolved. See `BUG_AUDIT.md` and the
code-review HTML artifact for the record.
