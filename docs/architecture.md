# Architecture

## Responsibilities

The package is `faq_agent`; responsibility folders live inside it to keep imports
unambiguous. HTTP routes validate requests and redact upstream failures. The shared
retrieval module uses `SupabaseLibrary` for all storage operations.
The hosted LLM client generates answers and validates citation IDs against the
retrieved evidence. Prompts are separate from client transport code.

The ingestion service owns the upload lifecycle. The loader is used by the CLI
corpus workflow; UI PDF extraction is page-aware. Both use the same chunker and
embedding client.

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
    A->>S: Load preview (id AND collection_id), duplicate check (checksum/content_hash/sha256)
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
    A->>L: Question and retrieved evidence
    L-->>A: Answer and citation IDs
    A-->>U: Validated answer or abstention
```

## Data and configuration

- Supabase (`rag` schema): all persistent state — collections, documents, chunk
  embeddings (pgvector), and expiring upload previews (`rag.previews`).  Run
  `docs/migrations/001_add_rag_previews.sql` once before deploying.
- `.env`: private runtime settings. `.env.example`: supported keys and defaults.
  `config.py` validates settings at startup. Existing retired keys in a private
  `.env` are ignored.
- Embedding model, revision, dimensions, prefixes and chunk configuration determine
  the collection fingerprint. Changing this profile requires a new index.
  The LLM model can change without re-embedding documents.
- Default embedding model: `BAAI/bge-small-en-v1.5`, 384 dimensions. The hosted
  URL must actually serve that model. Answer model selection is configurable via
  `LLM_MODEL`; provider access and credits must be checked separately.

## Contracts

Both `/search` and `/ask` accept `question`, optional `collection_id`, and optional
`document_id`. Document scope requires collection scope. `/ask` calls the shared
Python retrieval function, not the `/search` HTTP route. Empty retrieval skips the
LLM.

The UI only lists application-managed collections. Omitting `collection_id` selects
the configured default collection.

Approval is required before embedding. The duplicate check (by `sha256`,
`checksum`, or `content_hash`, in that order depending on which column is present
in the live schema) runs before the embedding API is called — re-uploading a known
document never incurs an embedding cost.

On the Supabase path, previews are persisted in `rag.previews` and survive server
restarts. Preview lookup filters on both `id` AND `collection_id` in a single SQL
query — a token from one collection cannot be used to probe another. A failed
commit leaves no orphaned rows because the document + chunk inserts run inside a
single explicit `connection.transaction()` block. The `preview_id` is retryable:
resubmitting after a transient failure re-embeds and re-writes cleanly.

The pgvector type adapter creates a fresh `Dumper` subclass per connection. The
`oid` is captured as a class attribute on that per-connection subclass so concurrent
connections never share mutable state.

Only committed documents are searchable.

## Limitations and next steps

Chunking uses sections and overlapping character windows, not an embedding model.
Default sizes are 1000 characters with 150 overlap; PDF pages are processed
separately. The preview shows planned dimensions, not a computed vector.

Similarity scores are not confidence estimates. Citation validation checks that
IDs exist in the evidence; factual quality needs evaluation. There is no active
reranking, conversational memory, Drive sync or monitoring stack. No per-IP rate
limiting is in place — add `slowapi` before public deployment to protect the
HuggingFace quota.

Next: evaluate real FAQs, then choose authentication, background jobs and
monitoring. Consider injecting a shared `SupabaseLibrary` via FastAPI `Depends()`
to eliminate per-request psycopg overhead under load.
