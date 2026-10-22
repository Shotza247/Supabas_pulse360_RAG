-- Migration 001: Add rag.previews table
-- Replaces the previous in-process _PREVIEWS dict with a durable,
-- server-restart-safe store in Supabase.  Run this once against your
-- Supabase project before deploying the updated supabase_store.py.
--
-- The application also calls CREATE TABLE IF NOT EXISTS at runtime, so
-- running this script is optional but recommended for explicit version
-- control of your schema changes.
--
-- Usage (psql):
--   psql "$SUPABASE_DATABASE_URL" -f docs/migrations/001_add_rag_previews.sql

-- ─────────────────────────────────────────────────────────────────────
-- 1. Create the table
-- ─────────────────────────────────────────────────────────────────────

create table if not exists rag.previews (
    -- Opaque 32-char hex token returned to the client as preview_id
    id            text        primary key,

    -- The collection this preview belongs to
    collection_id text        not null
        references rag.collections(id) on delete cascade,

    -- Original filename (sanitised, max 160 chars)
    filename      text        not null,

    -- SHA-256 hex of the raw file bytes — used for duplicate detection
    digest        text        not null,

    -- Serialised list of chunk dicts (chunk_id, text, metadata)
    chunks        jsonb       not null,

    -- Wall-clock creation time; used to enforce the 1-hour TTL
    created_at    timestamptz not null default now(),

    -- Set to true by commit() after a clean transaction write
    committed     boolean     not null default false
);

-- ─────────────────────────────────────────────────────────────────────
-- 2. Indexes
-- ─────────────────────────────────────────────────────────────────────

-- Allow the TTL cleanup query to run efficiently
create index if not exists previews_created_at_idx
    on rag.previews (created_at);

-- Allow quick lookup of all pending previews for a collection
create index if not exists previews_collection_id_idx
    on rag.previews (collection_id);

-- ─────────────────────────────────────────────────────────────────────
-- 3. Row-level security (matches the existing rag schema posture)
-- ─────────────────────────────────────────────────────────────────────

-- The table is accessed exclusively through the service-role connection
-- string (SUPABASE_DATABASE_URL).  RLS is enabled but no user-facing
-- policy is added; the service role bypasses RLS by default.
alter table rag.previews enable row level security;
