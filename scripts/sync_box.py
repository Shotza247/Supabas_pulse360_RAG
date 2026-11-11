"""Sync a Box file or folder into a Supabase collection.

Usage:
    uv run python scripts/sync_box.py --dry-run          # show what would change
    uv run python scripts/sync_box.py                    # sync for real
    uv run python scripts/sync_box.py --create-collection

Configuration comes from .env (see .env.example): BOX_CLIENT_ID, BOX_CLIENT_SECRET,
BOX_ENTERPRISE_ID (or BOX_DEVELOPER_TOKEN for a first test), BOX_FOLDER_ID or
BOX_SHARED_LINK, and BOX_COLLECTION. Credentials are never printed.
Exit code is 1 if configuration is incomplete or any file failed.
"""

from __future__ import annotations

import argparse
import sys

from faq_agent.config import get_settings
from faq_agent.integrations.box_source import BoxClient, BoxError, missing_box_settings
from faq_agent.integrations.box_sync import sync_box
from faq_agent.supabase_store import SupabaseLibrary


def resolve_root(box: BoxClient, folder_id: str, shared_link: str) -> tuple[dict, str | None]:
    """Return (box item to sync, shared link to send with requests or None)."""
    if folder_id:
        return {"id": folder_id, "type": "folder"}, None
    item = box.resolve_shared_link(shared_link)
    if item.get("type") not in {"file", "folder"}:
        raise BoxError(f"Shared link points at an unsupported item type: {item.get('type')}")
    return item, shared_link


def print_report(report: dict[str, list], dry_run: bool) -> None:
    print("DRY RUN - nothing was changed." if dry_run else "Sync complete.")
    for key, items in report.items():
        print(f"  {key}: {len(items)}")
        for item in items:
            print(f"    - {item}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--collection", help="Override BOX_COLLECTION")
    parser.add_argument("--folder-id", help="Override BOX_FOLDER_ID")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    parser.add_argument("--create-collection", action="store_true", help="Create it if missing")
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Allow pruning all stored files when Box lists none",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    collection = args.collection or settings.box_collection
    folder_id = args.folder_id or settings.box_folder_id
    shared_link = "" if args.folder_id else settings.box_shared_link
    missing = missing_box_settings(settings, folder_id=folder_id, shared_link=shared_link)
    if not collection:
        missing.append("BOX_COLLECTION")
    if missing:
        print("Missing configuration: " + ", ".join(missing), file=sys.stderr)
        return 1

    library, box = SupabaseLibrary(settings), BoxClient(settings)
    try:
        exists = collection in {c["id"] for c in library.list_collections()}
        if args.create_collection and not exists:
            if args.dry_run:
                print(f"DRY RUN - would create collection {collection}.")
            else:
                library.create(collection)
                print(f"Created collection {collection}.")
        root, link = resolve_root(box, folder_id, shared_link)
        report = sync_box(
            library,
            box,
            collection,
            root,
            shared_link=link,
            dry_run=args.dry_run,
            allow_empty=args.allow_empty,
        )
    except BoxError as exc:
        print(f"Box error: {exc}", file=sys.stderr)
        return 1
    print_report(report, args.dry_run)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
