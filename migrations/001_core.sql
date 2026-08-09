-- ============================================================================
-- 001 — core schema: run ledger, academic structure, and the canonical item table.
--
-- The role and database themselves are created by the postgres container from
-- POSTGRES_USER / POSTGRES_DB, so there is no 000_create_role.sql here.
-- Apply with: pap migrate   (tracked in pap.schema_migration)
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS pap;

-- --------------------------------------------------------------------------
-- source_run — one row per job execution. The monitor reads ONLY this table,
-- which is what keeps self-monitoring from drifting out of sync with reality.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pap.source_run (
    id              bigserial PRIMARY KEY,
    source          text        NOT NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    -- running | success | failed | skipped
    status          text        NOT NULL DEFAULT 'running',
    items_found     integer     NOT NULL DEFAULT 0,
    items_new       integer     NOT NULL DEFAULT 0,
    dry_run         boolean     NOT NULL DEFAULT false,
    error_text      text,
    -- Links a failed run straight to its stack trace in GlitchTip.
    sentry_event_id text
);

CREATE INDEX IF NOT EXISTS source_run_source_started_idx
    ON pap.source_run (source, started_at DESC);

-- --------------------------------------------------------------------------
-- Academic structure. Studeo groups disciplines into dated modules whose codes
-- look like "54/2025", "51/2026". `code` is stored EXACTLY as Studeo reports it;
-- year/seq are parsed out only for sorting and Drive foldering. If the numbering
-- turns out to mean something other than it appears to, only the parser changes.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pap.module (
    id               bigserial PRIMARY KEY,
    code             text NOT NULL UNIQUE,
    year             integer,
    seq              integer,
    label            text,
    drive_folder_id  text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pap.discipline (
    id               bigserial PRIMARY KEY,
    module_id        bigint REFERENCES pap.module (id) ON DELETE CASCADE,
    external_id      text NOT NULL,
    name             text NOT NULL,
    drive_folder_id  text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (module_id, external_id)
);

-- --------------------------------------------------------------------------
-- item — everything any adapter collects, in one canonical shape.
--
-- UNIQUE (source, external_id) is the idempotency guarantee: re-running any
-- scraper any number of times cannot create duplicates, because the constraint
-- refuses them rather than relying on the code remembering to check.
--
-- notified_at is what separates "seen" from "told you about" — the dispatcher
-- reads it, the scrapers only write items.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pap.item (
    id             bigserial PRIMARY KEY,
    source         text        NOT NULL,
    external_id    text        NOT NULL,
    content_hash   text        NOT NULL,
    kind           text        NOT NULL DEFAULT 'generic',
    title          text        NOT NULL,
    url            text,
    discipline_id  bigint      REFERENCES pap.discipline (id) ON DELETE SET NULL,
    payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    changed_at     timestamptz,
    notified_at    timestamptz,
    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS item_pending_notification_idx
    ON pap.item (source, first_seen_at) WHERE notified_at IS NULL;
CREATE INDEX IF NOT EXISTS item_kind_idx ON pap.item (source, kind);
