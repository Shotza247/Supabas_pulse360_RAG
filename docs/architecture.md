# Architecture

## Responsibilities

The package is `faq_agent`; responsibility folders live inside it to keep imports
unambiguous. HTTP routes validate requests and redact upstream failures. The shared
retrieval module selects either the UI collection or the configured CLI corpus.
The hosted LLM client generates answers and validates citation IDs against the
retrieved evidence. Prompts are separate from client transport code.

The ingestion service owns the upload lifecycle and SQLite catalog. The loader is
used by the versioned CLI workflow; UI PDF extraction is page-aware. Both use the
same chunker and embedding client. The vector-store adapters serve the CLI corpus;
the upload service uses Qdrant directly for its document-level lifecycle.

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
    A->>S: Load preview, duplicate check (checksum/content_hash)
    Note over A,S: Embedding API is NOT called for duplicates
    A->>E: Approved chunk text (new documents only)
    E-->>A: Validated vectors
    A->>S: BEGIN transaction — insert document + all chunks + mark preview committed
    Note over A,S: Full rollback on any failure; preview_id retryable
    S-->>U: Committed document record
    U->>A: Ask question with collection/document scope
    A->>E: Embed question
    A->>S: rag.match_document_chunks similarity search
    S-->>A: Chunks and vector scores
    A->>L: Question and retrieved evidence
    L-->>A: Answer and citation IDs
    A-->>U: Validated answer or abstention
```

## Data and configuration

- Supabase (`rag` schema): all persistent state for the production Supabase
  path — collections, documents, chunk embeddings (pgvector), and expiring
  upload previews (`rag.previews`).  Run
  `docs/migrations/001_add_rag_previews.sql` once before deploying.
- Qdrant + SQLite: still used by the local CLI corpus workflow.  SQLite stores
  collection identities, embedding profiles, committed document records and
  expiring previews for the Qdrant path only.
- `.env`: private runtime settings. `.env.example`: supported keys and defaults.
  config.py validates settings. Existing retired keys in a private .env are ignored;
  setup_env.py --sync removes unsupported keys after creating a private backup.
- Embedding model, revision, dimensions, prefixes and chunk configuration determine
  the existing collection fingerprint. Changing this profile requires a new index.
  The LLM model can change without re-embedding documents.
- Default embedding model: BAAI/bge-small-en-v1.5, 384 dimensions. The hosted URL
  must actually serve that model. Current answer model selection is configurable;
  provider access and included credits must be checked separately.
- `.local/` also holds current server logs and ignored refactor backups. No new
  logs directory is added simply to duplicate existing runtime storage.

## Contracts

Both /search and /ask accept question, optional collection_id, and optional
document_id. Document scope requires collection scope. Ask calls the shared Python
retrieval function, not the /search HTTP route. Empty retrieval skips the LLM.

The UI only lists application-managed collections. Omitting collection_id selects
the versioned CLI corpus from configuration. The optional Chroma adapter remains
available for that CLI path (`uv sync --extra chroma`); upload management requires
Qdrant. Other vector stores are not implemented.

Approval is required before embedding. The duplicate check (by checksum or
content_hash) runs before the embedding API is called — re-uploading a known
document never incurs an embedding cost.  On the Supabase path, previews are
persisted in `rag.previews` and survive server restarts; a failed commit leaves
no orphaned rows because the document + chunk inserts run inside a single
explicit `connection.transaction()` block.  The `preview_id` is retryable:
resubmitting after a transient failure re-embeds and re-writes cleanly.
Only committed documents are searchable. The catalog must be backed up
alongside the vector data. A collection is not a user-authorization boundary
in this local MVP.

## Limitations and next steps

Chunking uses sections and overlapping character windows, not an embedding model.
Default sizes are 1000 characters with 150 overlap; PDF pages are processed
separately. Whole FAQ question/answer pairs are not guaranteed. The preview shows
planned dimensions, not a computed vector. See ui-testing.md for size limits.

Similarity scores are not confidence estimates. Citation validation checks that
IDs exist in the evidence; factual quality needs evaluation. There is no active
reranking, LangGraph, conversational memory, Drive sync or monitoring stack.

Next: evaluate real FAQs, then choose authentication, durable metadata storage,
background jobs and deployment. Current SQLite/synchronous ingestion is not ready
for ephemeral Vercel instances. The API Dockerfile is a packaging option, not a
claim that production deployment has been validated.
