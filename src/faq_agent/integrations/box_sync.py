"""Sync Box files into a Supabase collection: add new, replace changed, prune removed.

A file is matched to its stored document through metadata written at commit time
(external_source / external_id / external_version / external_scope), so manual uploads
and other folders in the same collection are never touched.
"""

from __future__ import annotations

import logging
from pathlib import Path

from faq_agent.supabase_store import MAX_BYTES

logger = logging.getLogger("faq_agent")

SOURCE = "box"
SUPPORTED = {".pdf", ".txt", ".md", ".docx"}


def version_of(entry: dict) -> str:
    """Box changes sha1 when content changes; etag is the fallback for files without one."""
    return str(entry.get("sha1") or entry.get("etag") or "")


def _reason(exc: Exception) -> str:
    # HTTPException.detail and BoxError messages are ours and credential-free; for any
    # other exception only the type is reported so provider payloads never leak.
    detail = getattr(exc, "detail", None)
    if isinstance(detail, str):
        return detail
    return str(exc) if exc.__class__.__name__.startswith("Box") else type(exc).__name__


def sync_box(
    library,
    box,
    collection_id: str,
    root: dict,
    *,
    shared_link: str | None = None,
    dry_run: bool = False,
    allow_empty: bool = False,
) -> dict[str, list]:
    """Reconcile one Box file or folder with the collection and return a report.

    ``root`` is the Box item (``type`` file or folder) being synced. The full listing
    is read before anything is changed, so a Box outage can never look like deletions.
    """
    report: dict[str, list] = {
        k: [] for k in ("added", "updated", "unchanged", "removed", "skipped", "errors")
    }
    scope = str(root["id"])
    files = [root] if root.get("type") == "file" else list(box.iter_files(scope, shared_link))
    tracked = library.tracked_documents(collection_id, SOURCE, scope)
    seen: set[str] = set()

    for entry in files:
        file_id, name = str(entry["id"]), entry.get("name", "")
        seen.add(file_id)
        if Path(name).suffix.lower() not in SUPPORTED:
            report["skipped"].append(f"{name}: unsupported file type")
            continue
        if int(entry.get("size") or 0) > MAX_BYTES:
            report["skipped"].append(f"{name}: larger than {MAX_BYTES // (1024 * 1024)} MB")
            continue
        version, known = version_of(entry), tracked.get(file_id)
        if known and version and known["version"] == version:
            report["unchanged"].append(name)
            continue
        if dry_run:
            report["updated" if known else "added"].append(name)
            continue
        try:
            data = box.download(file_id, shared_link)
            preview = library.preview(collection_id, name, data)
            result = library.commit(
                collection_id,
                preview["preview_id"],
                True,
                metadata={
                    "external_source": SOURCE,
                    "external_id": file_id,
                    "external_version": version,
                    "external_scope": scope,
                    "external_name": name,
                },
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the sync
            logger.warning("box_sync_file_failed file_id=%s error=%s", file_id, type(exc).__name__)
            report["errors"].append(f"{name}: {_reason(exc)}")
            continue
        if result.get("status") == "already_stored":
            report["skipped"].append(f"{name}: identical content is already stored")
            continue
        if known:
            # Remove the old version only after the new one is safely stored.
            try:
                library.delete_document(collection_id, known["document_id"])
            except Exception as exc:  # noqa: BLE001
                report["errors"].append(
                    f"{name}: new version stored but old version not removed ({_reason(exc)})"
                )
                continue
            report["updated"].append(name)
        else:
            report["added"].append(name)

    stale = {fid: info for fid, info in tracked.items() if fid not in seen}
    if stale and not files and not allow_empty:
        report["errors"].append(
            f"Box listing was empty; refusing to remove {len(stale)} stored document(s) "
            "(pass --allow-empty if the folder was really emptied)"
        )
        return report
    for file_id, info in stale.items():
        if dry_run:
            report["removed"].append(file_id)
            continue
        try:
            library.delete_document(collection_id, info["document_id"])
            report["removed"].append(file_id)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"box file {file_id}: removal failed ({_reason(exc)})")
    return report
