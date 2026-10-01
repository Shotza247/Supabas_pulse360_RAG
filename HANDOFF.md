# New-chat handoff

## Scope and boundaries

Build Supabas_pulse360_RAG into a reusable Supabase PostgreSQL + pgvector template.
The user plans to use the existing Pulse360 Supabase project. Inspect its schema,
migration ownership and permissions before proposing additive changes. No database
connection or project credentials have been copied into this folder.

OpenSource-RAG-Template remains an independent Qdrant-focused project. Its cloud
demo/smoke migration passed, but fresh cloud upload, API retrieval and HF synthesis
have NOT been verified. Do not describe those pending milestones as completed.

## Inherited working capabilities

- FastAPI /health, /search, /ask and collection/document upload endpoints.
- Streamlit collection selection, upload preview, explicit embedding approval,
  search and cited answer display.
- PDF/TXT/MD upload extraction; CLI also supports DOCX.
- Section/page-aware character chunking, default 1000 characters and 150 overlap.
- Hosted HF embeddings; default BAAI/bge-small-en-v1.5, 384 dimensions.
- Shared retrieval for /search and /ask; hosted HF answer synthesis.
- SQLite managed collection/document/preview catalog and Qdrant storage.
- Optional Chroma CLI adapter; UI ingestion remains Qdrant-specific.
- No active reranking, LangGraph, Redis, role authorization or deployment validation.

## Implementation gates

1. Read the current code and tests. Confirm the target Supabase project and region
   without sharing secrets. Establish backup and migration ownership for Pulse360.
2. Design a private rag schema: collections, documents, chunks/embeddings and
   ingestion state. Preserve scope, source IDs, embedding profiles and deduplication.
3. Implement PostgreSQL catalog and pgvector storage. Refactor direct Qdrant calls
   in ingestion/service.py, not just the CLI vector-store adapter. Keep endpoint
   response contracts and convert cosine distance to documented similarity scores.
4. Preserve approved/committed-only retrieval. Do not hold a database transaction
   open across hosted embedding calls. Handle retries, failed uploads and expiry.
5. Use restricted server-side database credentials and serverless-compatible
   pooling for Vercel. Public FAQ access is not permission to edit documents or read
   private platform tables. Keep client secrets out of browser JavaScript.
6. Test fresh upload -> preview -> embed/store -> scoped search -> cited answer.
   Start with exact search as a baseline; choose indexes after measuring.
7. Deploy only after persistence, access controls, upload limits, credit limits,
   secrets, rollback and automated tests are verified.

For guesthouse/property applications, structured constraints such as availability,
price and capacity must be explicit filters; semantic similarity does not establish
booking availability. Keep this future domain work separate from the FAQ milestone.

## What was copied and excluded

Copied reusable source, offline tests, selected scripts, packaging, lockfile,
baseline guides and synthetic examples. Existing source behavior is unchanged.
Excluded .git, .env and cloud profiles, .venv, .local, SQLite files, snapshots,
private documents/PDFs, logs, cloud-specific migration scripts and credentials.
No independent repository commits, Supabase migrations or remote resources were created.

## Suggested first prompt

"Read README.md and HANDOFF.md in Supabas_pulse360_RAG. This is an isolated copy of
a working Qdrant/SQLite RAG baseline, not a functioning Supabase implementation yet.
Help me adapt its catalog and vector storage to the existing Pulse360 Supabase
project, step by step, preserving /search, /ask and the reviewed-upload workflow.
Do not change OpenSource-RAG-Template or access cloud data without confirming scope."
