---
name: change-audit
description: >
  Use when code changes have just been applied, a bug has been fixed, a refactor completed,
  or a commit is about to be made. Produces a structured audit record in BUG_AUDIT.md
  covering root cause, what changed, impact on the system, and how to verify the fix.
  Auto-activates after any code fix session. Triggers on: "audit this change", "record
  this fix", "document what changed", "write up the change", "what did we just fix",
  "log this fix", "update the audit log", or automatically after applying fixes before a commit.
---

# Change Audit Skill

Produce a complete, structured audit record for every meaningful code change so that future
agents, reviewers, and the team can understand *what* changed, *why*, and *how to verify* it
without re-reading the full conversation.

This skill writes to `BUG_AUDIT.md` in the project root.  Every record becomes a permanent,
queryable reference for any coding agent (Bob or otherwise) working on this project later.

---

## Step 1 — Identify what changed

Run `git diff HEAD~1 HEAD --stat` to get the exact list of changed files and line counts.
If changes are not yet committed, run `git diff --stat` instead.

Read only the files that were modified — do not re-read the whole codebase.

For each changed file note:
- **Category:** source (`src/`), test (`tests/`), config, docs, migration (`docs/migrations/`)
- **Net line delta** (additions vs deletions)
- **File status:** new file, modified, or deleted

---

## Step 2 — Classify the change

Assign **one** primary classification (use the first that fits):

| Label | When to use |
|---|---|
| `security` | Removes injection risk, fixes auth, adds input validation |
| `correctness` | Fixes a bug — wrong output, crash, silent failure |
| `reliability` | Adds retry logic, circuit breaker, explicit error handling |
| `data-integrity` | Fixes transaction, atomicity, duplicate, or consistency issue |
| `performance` | Reduces latency, memory, or unnecessary work |
| `refactor` | Same behaviour, cleaner structure — no functional change |
| `config` | Dependency, environment variable, or deployment file change |
| `docs` | Documentation only |
| `test` | Test additions or fixes with no source change |

Also assign a **severity** if the change fixed a defect:
`critical` · `high` · `medium` · `low` · `n/a` (for refactors, config, docs)

---

## Step 3 — Write the audit record

Append a new dated section to `BUG_AUDIT.md` using **exactly** this template.
Use `apply_diff` to append to an existing file; use `write_file` only if `BUG_AUDIT.md`
does not exist.

Insert the new section **at the top** of the file, directly after the `# Copy verification
and recovery` heading — newest entries first.

```markdown
## <YYYY-MM-DD> — <classification>(<severity>): <one-line title>

### Why this change was needed
<2–4 sentences describing the original symptom or risk. Be specific — reference the
observable failure or security concern, not just the solution.>

### Root cause
<The underlying reason the problem existed. One clear sentence.>

### What changed

| File | Status | Summary of change |
|------|--------|-------------------|
| `src/…` | modified | <what specifically changed inside the file> |
| `tests/…` | added | <what the new test covers> |
| `docs/…` | modified | <what documentation was updated> |

### System impact

| Dimension | Impact |
|-----------|--------|
| Data quality | <how stored data is now more correct, or "no impact"> |
| Retrieval quality | <how search / answer results improve, or "no impact"> |
| Security | <what attack surface was reduced, or "no impact"> |
| Reliability | <how failure handling improved, or "no impact"> |
| Performance | <latency / cost improvement, or "no impact"> |

### How to verify
<One concrete, runnable check — a specific test name, curl command, or SQL query.
Must be executable without additional context.>

### Linked commit
`<7-char git hash>` — `<full first line of commit message>`

### Follow-up items
- <Any remaining risk, known limitation, or next step — be honest; write "None" if clean>
```

---

## Step 4 — Validate before writing

Before calling `apply_diff`, check every item:

- [ ] Every file that changed is listed in the "What changed" table
- [ ] "Why this change was needed" describes the symptom, not just the fix
- [ ] "How to verify" is runnable by anyone with a cloned repo and valid `.env`
- [ ] The linked commit hash is real — confirm with `git log --oneline -1`
- [ ] No credentials, tokens, connection strings, or internal URLs appear anywhere
- [ ] The "Follow-up items" bullet is present (even if it says "None")

If any check fails, correct the record before writing.

---

## Step 5 — Report back

After writing, confirm in **one short paragraph**:
- What classification and severity was assigned
- The exact section heading that was appended
- Whether any follow-up items were noted

Do **not** re-print the full record — just confirm it was written.

---

## Notes for future agents

- This skill is intentionally narrow: one change = one record.  Do not batch multiple
  unrelated fixes into a single entry.
- The "System impact" table dimensions (data quality, retrieval quality, security,
  reliability, performance) are specific to this Supabase pgvector RAG project.
  Adjust the dimensions if the project domain changes significantly.
- If `doc-sync` is available, invoke it after writing the audit record to keep
  `docs/architecture.md` and `README.md` aligned with the change.
