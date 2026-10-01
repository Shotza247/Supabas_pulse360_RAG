# Supabase pgvector smoke test

The reusable smoke-test page is `supabase_pgvector_test.py`. The main
`streamlit_app.py` now also uses the direct Supabase PostgreSQL path. Both avoid
the old Qdrant/SQLite upload API and write to the private Supabase `rag` schema,
so the schema does not need to be exposed through the Supabase REST API.

The smoke-test page is intentionally narrower: it writes only to
`test_collection` and marks its documents with a cleanup prefix. The main page
can select any collection registered in `rag.collections`, including
`pulse360_faq`.

## One-time setup

Create or update this private file:

`C:\Users\Jabulani Ndlovu\Downloads\Project Folder\Project Folder\Idea_Not_Started\Supabas_pulse360_RAG\.env`

Add these values:

```dotenv
SUPABASE_DATABASE_URL=postgresql://postgres:URL_ENCODED_PASSWORD@db.<project-ref>.supabase.co:5432/postgres?sslmode=require
SUPABASE_TEST_COLLECTION=test_collection
```

Replace `URL_ENCODED_PASSWORD` with the real database password. If the password
contains characters such as `@`, `#`, `:` or `/`, URL-encode them first. Keep
the database URL private. This variable is used by the direct PostgreSQL test UI;
it is different from Pulse360's application `DATABASE_URL`.

Install the new dependency and start the UI:

```powershell
uv sync --extra ui
uv run streamlit run supabase_pgvector_test.py --server.address 127.0.0.1 --server.port 8503 --browser.gatherUsageStats false
```

Open `http://127.0.0.1:8503` and upload either the included
`examples/supabase-pgvector-smoke-test.md` or another small PDF, TXT, or Markdown
file. The UI will:

1. Extract text and create chunks locally.
2. Generate 384-dimensional embeddings through the configured embedding service.
3. Insert a committed document and its vectors into `rag.document_chunks`.
4. Call `rag.match_document_chunks` to verify similarity retrieval.

The UI uses the reserved filename prefix `__pgvector_test__/` so cleanup is
narrowly scoped. It does not drop or recreate `test_collection`.

## Cleanup

Delete all reusable smoke-test documents and their embeddings:

```powershell
uv run python scripts/delete_supabase_test_document.py
```

Delete one known smoke-test document only:

```powershell
uv run python scripts/delete_supabase_test_document.py --document-id <document-uuid>
```

The script deletes rows from `rag.document_chunks` first, then matching rows
from `rag.documents`, and leaves `rag.collections` and `test_collection` intact.

## Expected schema contract

The tool expects these columns, which match the current Pulse360 pgvector
design:

- `rag.documents`: `id`, `collection_id`, `filename`, `category`, `status`
- `rag.document_chunks`: `id`, `document_id`, `content`, `embedding`

Optional columns such as `checksum`, `metadata`, `chunk_index`, `page_number`,
`embedding_model`, and `embedding_dimensions` are populated when present. The
UI checks the live schema before writing and reports missing required columns.
