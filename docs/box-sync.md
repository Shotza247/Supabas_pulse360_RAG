# Box document sync

`scripts/sync_box.py` keeps a Supabase collection in step with a Box file or folder:
new files are added, changed files are replaced, files removed from Box are pruned.
Only documents the sync itself created are ever replaced or pruned (it tags them with
`external_source=box`, `external_id`, `external_version` and `external_scope` in the
document metadata), so manual uploads and other folders in the same collection are safe.

Supported types: PDF, DOCX, TXT, Markdown. Files over 10 MB, or that produce more than
100 chunks, are skipped and reported.

## One-time Box setup (Box admin)

1. Box Developer Console > Create Platform App > Custom App.
2. Authentication method: **Server Authentication (Client Credentials Grant)**.
   JWT also works in Box, but this sync does not implement it; it needs a keypair and
   a JSON config file, and Client Credentials gives the same server-to-server access
   with just an ID and a secret.
3. Configuration tab > Application Scopes: enable **Read all files and folders stored in Box**.
4. Authorization tab > Submit for authorization, then approve it in
   Admin Console > Integrations > Platform Apps Manager.
5. Either **share the content with the app**: invite the app's service-account email
   (General Settings tab) as a *Viewer* on the folder, or use a shared link.
   A shared link works without an invite as long as it is open to the app's account.
6. Copy Client ID and Client Secret (Configuration tab) and the Enterprise ID
   (Admin Console > Account & Billing).

## Configure

Fill these in `.env` (never commit it). The same names are used as GitHub secrets/variables:

| Variable | Notes |
|---|---|
| `BOX_CLIENT_ID`, `BOX_CLIENT_SECRET`, `BOX_ENTERPRISE_ID` | Client Credentials Grant |
| `BOX_DEVELOPER_TOKEN` | Optional 60-minute token for a first manual test instead of the three above |
| `BOX_FOLDER_ID` | Number in `app.box.com/folder/<id>` |
| `BOX_SHARED_LINK` | Used when `BOX_FOLDER_ID` is empty; resolved to a file or folder |
| `BOX_COLLECTION` | Target collection, e.g. `test_collection_faq` |

## Run

```
uv run python scripts/sync_box.py --dry-run            # report only, nothing written
uv run python scripts/sync_box.py --create-collection  # first real run
uv run python scripts/sync_box.py                      # later runs
```

Exit code is 1 when configuration is incomplete or any file failed. Credentials are never printed.

Safety behaviour:
- The whole Box listing is read before anything changes, and an empty listing never
  deletes stored documents unless `--allow-empty` is passed.
- A changed file's old version is deleted only after the new version is stored.
- One failing file does not stop the rest.

## Scheduled sync

`.github/workflows/box-sync.yml` runs hourly but stays off until the repository variable
`BOX_SYNC_ENABLED` is `true`. Add secrets `SUPABASE_DATABASE_URL`, `HF_TOKEN`,
`EMBEDDING_URL`, `BOX_CLIENT_ID`, `BOX_CLIENT_SECRET`, `BOX_ENTERPRISE_ID` (and
`BOX_SHARED_LINK` if used) and variables `BOX_FOLDER_ID`, `BOX_COLLECTION`.
Run it manually first (Actions > Box sync > Run workflow); manual runs default to a dry run.

## Limits

- Polling, not webhooks: changes appear on the next run.
- Box permissions are not carried over. Everything synced into a collection is visible to
  anyone allowed to query that collection (`PUBLIC_COLLECTIONS`, `QUERY_API_KEY`).
- Document text is sent to the configured embedding and LLM providers.
- Tested against a fake Box client only. The first run against real Box is unverified.
