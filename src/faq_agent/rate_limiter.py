"""Distributed fixed-window rate limiter backed by a Supabase table.

Falls back to an in-process counter when no database URL is configured
(tests, local dev without a live DB).

The table is created idempotently on first use — no separate migration
required.  One row per (ip, window) pair; the count is atomically
incremented with INSERT … ON CONFLICT DO UPDATE so concurrent serverless
invocations (Vercel, Gunicorn workers) all share the same counter.

Window: 60 seconds.  Limit: 30 requests per IP per window.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict

logger = logging.getLogger("faq_agent")

RATE_LIMIT = 30   # max requests per IP per window
RATE_WINDOW = 60  # seconds

# In-process fallback (single-process / test environments)
_local_counters: dict[tuple[str, int], int] = defaultdict(int)

_DDL = """
create table if not exists rag.rate_limits (
    ip         text    not null,
    window     bigint  not null,
    count      integer not null default 0,
    primary key (ip, window)
);
create index if not exists rate_limits_window_idx on rag.rate_limits (window);
"""

_UPSERT = """
insert into rag.rate_limits (ip, window, count)
values (%s, %s, 1)
on conflict (ip, window)
do update set count = rag.rate_limits.count + 1
returning count;
"""

_CLEANUP = "delete from rag.rate_limits where window < %s"


def _current_window() -> int:
    return int(time.monotonic() // RATE_WINDOW)


def check(ip: str, database_url: str | None = None) -> bool:
    """Return True if the request is allowed, False if it exceeds the limit.

    Tries the Supabase path first; on any DB error falls back to the
    in-process counter so a temporary DB hiccup does not take down the API.
    """
    url = database_url or os.getenv("SUPABASE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if url:
        try:
            return _check_db(ip, url)
        except Exception:  # noqa: BLE001 — degrade gracefully, do not crash the request
            logger.warning("rate_limit_db_failed ip=%s — falling back to in-process counter", ip)
    return _check_local(ip)


def _check_db(ip: str, database_url: str) -> bool:
    import psycopg  # imported here so the module loads without psycopg installed

    window = _current_window()
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            # Ensure the table exists (idempotent — cheap if it already exists)
            cur.execute(_DDL)
            # Atomically increment and read back the new count
            cur.execute(_UPSERT, (ip, window))
            count = cur.fetchone()[0]
            # Prune old windows (best-effort; ignore errors)
            try:
                cur.execute(_CLEANUP, (window - 2,))
            except Exception:  # noqa: BLE001
                pass
    return count <= RATE_LIMIT


def _check_local(ip: str) -> bool:
    window = _current_window()
    key = (ip, window)
    _local_counters[key] += 1
    # Prune old windows
    for old in [k for k in _local_counters if k[1] < window - 1]:
        del _local_counters[old]
    return _local_counters[key] <= RATE_LIMIT


def clear_local() -> None:
    """Test helper — reset the in-process fallback counter."""
    _local_counters.clear()
