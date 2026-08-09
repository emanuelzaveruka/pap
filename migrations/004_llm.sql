-- ============================================================================
-- 004 — LLM call ledger (Phase 2).
--
-- Every call through the LLMProvider port lands here regardless of vendor, so
-- comparing Claude / OpenAI / Gemini on real cost and latency is a SQL query
-- rather than guesswork. `purpose` is the task name the router resolved on
-- (deliverable, book_resume, classify), NOT the vendor — the domain never learns
-- which provider answered.
-- ============================================================================

CREATE TABLE IF NOT EXISTS pap.llm_call (
    id             bigserial PRIMARY KEY,
    purpose        text NOT NULL,
    provider       text NOT NULL,
    model          text NOT NULL,
    input_tokens   integer,
    output_tokens  integer,
    latency_ms     integer,
    ok             boolean NOT NULL DEFAULT true,
    error_text     text,
    run_id         bigint REFERENCES pap.source_run (id) ON DELETE SET NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS llm_call_created_idx ON pap.llm_call (created_at DESC);
CREATE INDEX IF NOT EXISTS llm_call_provider_idx ON pap.llm_call (provider, purpose);
