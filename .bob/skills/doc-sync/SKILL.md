---
name: doc-sync
description: >
  Use when documentation is out of date after a code change, when architecture has shifted,
  when new environment variables were added or removed, when a migration file was added,
  or when preparing to commit. Keeps BUG_AUDIT.md, docs/architecture.md, README.md, and
  .env.example in sync with the actual codebase. Auto-activates when a significant code
  change session ends. Triggers on: "update the docs", "docs are out of date", "sync
  documentation", "README needs updating", "update architecture", "what docs need to
  change", "are the docs current", or after any session that modifies source files.
---

# Doc-Sync Skill

Keep every documentation file truthful and useful after code changes.
A doc that describes a deleted file, a removed env var, or an old architectural choice is
**worse than no doc at all** — it misleads both human developers and future AI agents.

This skill is composable with `change-audit`.  If the audit record for the current change
is missing from `BUG_AUDIT.md`, activate `change-audit` first before running this skill.

---

## Step 1 — Determine what changed since the last doc update

Run `git log --oneline -10` to see recent commits.

Identify the range since the last doc-touching commit (look for commits whose message
starts with `docs:` or contains "architecture", "README", "BUG_AUDIT").

Run `git diff <last-doc-commit>..HEAD --name-only` to get the full list of changed source
files.  Read only the files relevant to documentation — do not re-read the whole codebase.

**Files that drive documentation in this project:**

| Source file | Drives |
|---|---|
| `src/faq_agent/config.py` | `.env.example` · README config section |
| `src/faq_agent/supabase_store.py` | `docs/architecture.md` data flow + sequence diagram |
| `src/faq_agent/api/routes.py` | README endpoint table |
| `src/faq_agent/api/documents.py` | README endpoint table |
| `src/faq_agent/retrieval/retriever.py` | `docs/architecture.md` sequence diagram |
| `docs/migrations/*.sql` | `docs/architecture.md` migrations table |
| `vercel.json` | README deployment section |
| `pyproject.toml` | README installation section · dependency list |

---

## Step 2 — Audit each documentation file

For each file below, answer the specific questions before making any edits.
**Only edit what the code change requires** — do not reorganise or rephrase existing content.

### BUG_AUDIT.md

- Does every entry have a linked commit hash?  If not, look up the hash with
  `git log --oneline --all | grep "<title keywords>"` and add it.
- Are there items marked "Not yet verified" that are now verified?  Update the status.
- Is the most recent code change recorded?  If not, stop and activate `change-audit` first.

### docs/architecture.md

- Does the Mermaid sequence diagram match the actual call flow in `retriever.py` and
  `supabase_store.py`?  Compare participants and arrows against the real functions.
- Do all `participant` labels in the diagram exist in at least one `→` or `-->>` arrow?
  (A declared but unused participant breaks rendering.)
- Does the "Data and configuration" section list `rag.previews` (added in migration 001)?
- Does the "Contracts" section describe the current C1–C3 guarantees:
  durable previews, pre-embed duplicate check, atomic transaction?
- Are Qdrant, Chroma, SQLite, and the local `Library` class absent from all sections?

### README.md

- Does the configuration / environment variables section list every field in
  `src/faq_agent/config.py`'s `Settings` dataclass — and no removed fields?
- Are these 7 Vercel environment variables documented in the deployment section:
  `SUPABASE_DATABASE_URL`, `HF_TOKEN`, `EMBEDDING_URL`, `LLM_MODEL`,
  `COLLECTION`, `ALLOWED_ORIGINS`, `APP_ENV`?
- Does the endpoint table (if present) include all 6 routes:
  `GET /health`, `POST /search`, `POST /ask`,
  `GET /collections`, `POST /collections/{id}/uploads/preview`,
  `POST /collections/{id}/documents`?
- Does the installation command reference `uv sync --extra dev`
  (not `--extra chroma` or any Qdrant-related extra)?

### .env.example

- Does every key in the file correspond to a field in `Settings` in `config.py`?
- Are these removed keys **absent**: `VECTOR_STORE`, `QDRANT_PORT`, `CHROMA_PORT`,
  `CORPUS_VERSION`, `VECTOR_URL`, `VECTOR_API_KEY`?
- Do all section comments accurately describe the keys below them?

---

## Step 3 — Make the minimal edits

For each file that needs updating:

1. Use `apply_diff` — never rewrite a whole documentation file unless it is brand new
   or more than 50% of its content is wrong.
2. Make only edits that are **directly caused by the code changes** identified in Step 1.
   Do not improve phrasing, reorganise sections, or add new content beyond what the
   code change requires.
3. Every edit must be traceable: if a sentence is added, name the code change that caused it.

---

## Step 4 — Update the migration index (when a new migration was added)

If a new file exists under `docs/migrations/` that is not yet listed in `docs/architecture.md`,
add it to a `## Migrations` table (create the table if it does not exist, place it before
`## Limitations and next steps`):

```markdown
## Migrations

| File | Purpose |
|------|---------|
| `docs/migrations/001_add_rag_previews.sql` | Add rag.previews table for durable upload session storage |
```

---

## Step 5 — Validate before finishing

For each file that was edited, verify:

- [ ] No deleted symbol, file, env var, endpoint, or service is still referenced
- [ ] No placeholder text (`<your value>`, `TODO`, `…`) was left in
- [ ] Every Mermaid `participant` declared in `architecture.md` appears in at least one arrow
- [ ] All code blocks use the correct language tag (`sql`, `python`, `bash`, `json`, `dotenv`)
- [ ] No credentials, tokens, connection strings, or private URLs appear in any doc file
- [ ] `.env.example` keys are alphabetically grouped within their sections

---

## Step 6 — Suggest a doc-only commit if significant

If two or more doc files were updated, suggest (but do not execute) a commit:

```
docs: sync documentation after <short description of the code changes>

- <filename>: <one line — what changed and why>
- <filename>: <one line — what changed and why>
```

Present the suggested message to the developer for approval before committing.

---

## Step 7 — Report back

Summarise in a short bullet list:
- Which files were **edited** and the one-line reason for each edit
- Which files were **checked and already up to date**
- Whether a doc commit was suggested

Do **not** re-print diffs or full file contents — the summary list is enough.

---

## Notes for future agents

- This skill is intentionally **conservative** — minimal, traceable edits only.
  If you find yourself rewriting large sections, stop and confirm scope with the developer.
- The specific field names, table names, and Vercel env vars named in Step 2 are grounded
  in the current state of this project (Supabase pgvector, Vercel, HuggingFace hosted models).
  Update the audit questions in Step 2 if the architecture changes significantly.
- `change-audit` and `doc-sync` compose: run `change-audit` first (records *what* changed),
  then `doc-sync` (makes the docs reflect *what* changed).  Together they form a complete
  post-change workflow suitable for automation in CI.
