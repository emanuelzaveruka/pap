-- ============================================================================
-- 003 — the Central Notifications queue.
--
-- Scrapers never send anything; they write pap.item rows. The dispatcher turns
-- new items into notifications. This separation is what stops a re-run from
-- re-alerting, and it is why every channel shares one dedupe and retry policy.
-- ============================================================================

CREATE TABLE IF NOT EXISTS pap.notification (
    id          bigserial PRIMARY KEY,
    -- The whole trustworthiness of Central Notifications rests on this column:
    -- the UNIQUE violation on insert IS the dedupe, so a duplicate cannot be
    -- sent even if two jobs race. Do not replace it with a SELECT-then-INSERT.
    dedupe_key  text NOT NULL UNIQUE,
    channel     text NOT NULL,              -- telegram | email
    title       text NOT NULL,
    body        text NOT NULL DEFAULT '',
    url         text,
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- pending | sent | failed
    state       text NOT NULL DEFAULT 'pending',
    attempts    integer NOT NULL DEFAULT 0,
    last_error  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz
);

CREATE INDEX IF NOT EXISTS notification_pending_idx
    ON pap.notification (created_at) WHERE state = 'pending';
