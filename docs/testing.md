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

Tests use simulated providers and in-memory Qdrant, not HF credits. They cover
request validation, error redaction, citation validation, chunk bounds, embedding
dimensions, corpus isolation, upload approval, duplicate handling, preview expiry,
partial commit recovery, UI controls and configuration compatibility.

The UI uses native text elements for document lists: this machine's Application
Control policy rejects PyArrow's data-table runtime. No security policy changes
are necessary for the current UI.

## Live checks

1. Confirm GET /health returns 200; it is liveness, not a provider readiness check.
2. Confirm GET /collections lists managed collections and that the selected
   collection's documents remain visible after a restart.
3. Select ui_smoke_test in Streamlit and ask "What does Project Atlas show?".
   Search should return the synthetic chunk; Answer should cite it. These calls
   consume hosted credits. Use included credits only.
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

## CLI corpus

For the separate versioned-corpus workflow, local inspection requires no approval:

```powershell
.\.venv\Scripts\python.exe scripts/ingest_faq.py --source examples/faq --version review-001 --dry-run
```

To actually ingest, use a new version and explicitly pass --approved-public only
after reviewing all files in the source folder. This CLI does not register UI
documents. Prefer the UI for individual collection-scoped uploads.
