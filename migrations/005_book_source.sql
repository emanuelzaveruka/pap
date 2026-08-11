-- ============================================================================
-- 005 — give pap.book a stable identity and download bookkeeping.
--
-- The original table had no external key, so nothing stopped a second run from
-- inserting the same book again. Studeo identifies a livro didático by idApostila
-- (stable per discipline offering), so that becomes the dedupe key.
-- ============================================================================

ALTER TABLE pap.book
    ADD COLUMN IF NOT EXISTS external_id   text,
    ADD COLUMN IF NOT EXISTS filename      text,
    ADD COLUMN IF NOT EXISTS mime_type     text,
    ADD COLUMN IF NOT EXISTS bytes         bigint,
    ADD COLUMN IF NOT EXISTS downloaded_at timestamptz,
    ADD COLUMN IF NOT EXISTS uploaded_at   timestamptz;

-- UNIQUE (discipline_id, external_id) is what makes `pap archive books` safe to
-- re-run: the second attempt collides instead of duplicating. Partial, because
-- rows created before this migration have no external_id and must not all collide
-- with each other on NULL.
CREATE UNIQUE INDEX IF NOT EXISTS book_discipline_external_idx
    ON pap.book (discipline_id, external_id)
    WHERE external_id IS NOT NULL;

-- Content-level dedupe: the same PDF is often the livro for several offerings of a
-- discipline. Matching on hash means it is downloaded and uploaded once.
CREATE INDEX IF NOT EXISTS book_sha256_idx ON pap.book (sha256) WHERE sha256 IS NOT NULL;

-- Drives the work queue: books still needing bytes or a Drive copy.
CREATE INDEX IF NOT EXISTS book_pending_idx ON pap.book (created_at)
    WHERE downloaded_at IS NULL OR uploaded_at IS NULL;
