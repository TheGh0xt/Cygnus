-- ⚠️  SUPERSEDED — DO NOT APPLY. Superseded by 2026-09-07-beta-schema.sql.
--
-- This migration was written but never applied. It was also wrong: it ADDs
-- prompt_tokens/response_tokens/total_tokens, but analysis_usage already
-- carried input_tokens/output_tokens, so applying it would have left five
-- token columns of which two were dead. The replacement renames instead.
--
-- Kept for the record, because the gap it left is worth remembering: the code
-- wrote column names that did not exist, record_usage swallowed PostgREST's
-- rejection, and cost data silently collected nothing for seventeen days while
-- every analysis reported success.
--
-- ROADMAP 5.6 — cost per analysis.
--
-- Apply to the Supabase Postgres (SQL editor or CLI). Until it is applied,
-- Cygnus keeps working: record_usage catches PostgREST's rejection of the
-- unknown columns and logs it, so analyses still run and still persist, but
-- no cost data accumulates.
--
-- Why this matters now: the generation cycle spends money unattended, eight
-- analyses a day, and that budget was set against an unmeasured unit price.
-- Two days of real numbers turns it from a guess into a decision. Phase 6.1
-- then needs the same data to prove margin is positive on the heaviest
-- realistic user, so this cannot wait for monetization to start.
--
-- Nullable on purpose. Rows written before this migration have no token
-- counts and never will; a NOT NULL default of 0 would make them
-- indistinguishable from analyses that genuinely cost nothing, quietly
-- dragging the average cost down.

ALTER TABLE analysis_usage
    ADD COLUMN IF NOT EXISTS prompt_tokens   integer,
    ADD COLUMN IF NOT EXISTS response_tokens integer,
    ADD COLUMN IF NOT EXISTS total_tokens    integer;

-- Cost reporting reads recent rows by time, not by user.
CREATE INDEX IF NOT EXISTS idx_analysis_usage_created
    ON analysis_usage (created_at);

-- After applying, confirm the cost record is actually filling:
--
--   select date_trunc('day', created_at) as day,
--          count(*) as analyses,
--          round(avg(total_tokens)) as avg_tokens,
--          sum(total_tokens) as tokens
--     from analysis_usage
--    where total_tokens is not null
--    group by 1 order by 1 desc;
--
-- An empty result after a cycle has run means the columns are still missing
-- or the write is failing — check the Cygnus logs for "failed to record
-- usage". Do not read silence as success.
