---
noteId: "8ddceac0b10e11f183f7f156305e3c6d"
tags: []

---

# Testing and evaluation

## Offline tests

Install dev and UI extras, then run from the project root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src scripts tests main.py streamlit_app.py
```

Tests use simulated providers and mocked database access; they do not require a
live Supabase database or consume HF credits. They cover request validation,
rate limiting, error redaction, citation validation, chunk bounds, embedding
dimensions, upload approval, UI controls and configuration compatibility.

The UI uses native text elements for document lists: this machine's Application
Control policy rejects PyArrow's data-table runtime. No security policy changes
are necessary for the current UI.

## Live checks

1. Confirm GET /health returns 200; it is liveness, not a provider readiness check.
2. Confirm authenticated GET /collections lists collections and that the selected
   collection's documents remain visible after a restart.
3. Select a collection in Streamlit and ask a question grounded in a reviewed
   document. Search should return matching chunks; Answer should cite them.
   Hosted calls may consume credits.
4. For new documents, preview locally before approval. Verify filename, pages and
   chunk boundaries. Then approve and embed deliberately.
5. Test answerable, unanswerable and misleading questions against reviewed facts.

The earlier smoke fixture is one synthetic chunk, not an accuracy benchmark.
The Pulse360 PDF was locally previewed during UI setup (30 chunks over 10 pages);
its current ingestion status is shown by the UI, not assumed from this document.

## Retrieval metrics

Create a reviewed JSON array using chunk IDs returned by /search:

```json
[{"question":"Your FAQ question", "expected_chunk_ids":["a-real-chunk-id"]}]
```

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_retrieval.py labels.json --collection pulse360_faq
```

The evaluator calls the active /search endpoint and reports mean recall at the
configured CONTEXT_K and mean reciprocal rank. It makes embedding calls, not LLM
calls. Labels are collection-specific; re-chunking changes chunk IDs. Evaluate
answer correctness and source support separately from retrieval ranking.
