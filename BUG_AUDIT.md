# Copy verification and recovery

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
