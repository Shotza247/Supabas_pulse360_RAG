-- Migration 002: Add rag.rate_limits table
-- Replaces the previous in-process fixed-window counter with a durable,
-- shared counter visible to all Vercel serverless invocations and Gunicorn
-- workers.  Run this once against your Supabase project before deploying
-- to a multi-worker or serverless environment.
--
-- The application also calls CREATE TABLE IF NOT EXISTS at runtime via
-- rate_limiter.py, so running this script is optional but recommended for
-- explicit version control of your schema changes.
--
-- Usage (psql):
--   psql "$SUPABASE_DATABASE_URL" -f docs/migrations/002_add_rate_limits.sql

-- ─────────────────────────────────────────────────────────────────────
-- 1. Create the table
-- ─────────────────────────────────────────────────────────────────────

create table if not exists rag.rate_limits (
    -- Client IP address
    ip         text    not null,

    -- Fixed-window bucket: floor(unix_monotonic / 60)
    window     bigint  not null,

    -- Running request count for this (ip, window) pair
    count      integer not null default 0,

    primary key (ip, window)
);

-- ─────────────────────────────────────────────────────────────────────
-- 2. Index
-- ─────────────────────────────────────────────────────────────────────

-- Allows the TTL cleanup query (DELETE WHERE window < ?) to run efficiently
create index if not exists rate_limits_window_idx
    on rag.rate_limits (window);

-- ─────────────────────────────────────────────────────────────────────
-- 3. Row-level security
-- ─────────────────────────────────────────────────────────────────────

-- Accessed exclusively via the service-role connection string.
-- RLS is enabled; the service role bypasses it by default.
alter table rag.rate_limits enable row level security;
