-- ============================================================================
-- 002 — deadlines, attachments, books and generated deliverables.
-- Tables used from Phase 1 (deadline/attachment) through Phase 4 (book_resume).
-- ============================================================================

-- --------------------------------------------------------------------------
-- deadline — an activity's prazo, and the Google Calendar event mirroring it.
-- gcal_event_id is the sync key: present means "already in Calendar", so a moved
-- prazo PATCHes the existing event instead of creating a second one.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pap.deadline (
    id             bigserial PRIMARY KEY,
    item_id        bigint NOT NULL REFERENCES pap.item (id) ON DELETE CASCADE,
    due_at         timestamptz NOT NULL,
    gcal_event_id  text,
    synced_at      timestamptz,
    synced_due_at  timestamptz,   -- the due_at that was last pushed; differs => needs a patch
    UNIQUE (item_id)
);

CREATE INDEX IF NOT EXISTS deadline_pending_sync_idx
    ON pap.deadline (due_at) WHERE gcal_event_id IS NULL;

-- --------------------------------------------------------------------------
-- attachment — course material referenced by an activity.
-- sha256 dedupes by content, so the same PDF attached to two activities is
-- downloaded and uploaded once.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pap.attachment (
    id             bigserial PRIMARY KEY,
    item_id        bigint NOT NULL REFERENCES pap.item (id) ON DELETE CASCADE,
    filename       text NOT NULL,
    source_url     text NOT NULL,
    local_path     text,
    sha256         text,
    bytes          bigint,
    drive_file_id  text,
    downloaded_at  timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (item_id, source_url)
);

-- --------------------------------------------------------------------------
-- book — a book pulled from Studeo's library, and the state of its resume feed.
-- status: pending | active | completed | failed
-- config holds per-book overrides of the RESUME_* defaults, so a book can be
-- tuned (unit size, target length, style) without touching code or .env.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pap.book (
    id             bigserial PRIMARY KEY,
    discipline_id  bigint REFERENCES pap.discipline (id) ON DELETE SET NULL,
    title          text NOT NULL,
    source_url     text,
    local_path     text,
    sha256         text,
    drive_file_id  text,
    total_units    integer,
    current_pass   integer NOT NULL DEFAULT 1,
    status         text NOT NULL DEFAULT 'pending',
    config         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    completed_at   timestamptz
);

-- --------------------------------------------------------------------------
-- book_resume — one delivered unit of the serial feed.
--
-- UNIQUE (book_id, pass_number, unit_index) is what makes double-delivery
-- structurally impossible: a retry, a duplicate timer fire or a manual re-run
-- collides with the constraint instead of sending the same resume twice.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pap.book_resume (
    id              bigserial PRIMARY KEY,
    book_id         bigint NOT NULL REFERENCES pap.book (id) ON DELETE CASCADE,
    pass_number     integer NOT NULL DEFAULT 1,
    unit_index      integer NOT NULL,
    unit_label      text,
    text            text NOT NULL,
    provider        text,
    model           text,
    prompt_version  text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz,
    UNIQUE (book_id, pass_number, unit_index)
);

-- --------------------------------------------------------------------------
-- deliverable — a document generated for an activity from a pattern spec.
-- pattern_version is recorded so a regenerated file always traces back to the
-- exact spec that produced it.
-- status: draft | rendered | uploaded | failed
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pap.deliverable (
    id               bigserial PRIMARY KEY,
    item_id          bigint NOT NULL REFERENCES pap.item (id) ON DELETE CASCADE,
    pattern_name     text NOT NULL,
    pattern_version  text NOT NULL,
    fmt              text NOT NULL DEFAULT 'pdf',
    local_path       text,
    drive_file_id    text,
    provider         text,
    model            text,
    status           text NOT NULL DEFAULT 'draft',
    generated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (item_id, pattern_name, fmt)
);
