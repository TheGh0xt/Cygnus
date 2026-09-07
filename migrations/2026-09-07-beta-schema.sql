-- ROADMAP §0b — the beta push (B.6, B.9, B.10, B.15, B.19), plus a live fix.
--
-- ─────────────────────────────────────────────────────────────────────────
-- PART 1 — the token columns never existed, and nothing said so.
-- ─────────────────────────────────────────────────────────────────────────
--
-- migrations/2026-08-21-usage-token-columns.sql was written but never applied.
-- Meanwhile src/api/usage.py ships TokenUsage with prompt_tokens /
-- response_tokens / total_tokens, and analysis_usage carries input_tokens /
-- output_tokens — two names for one idea, neither matching the code.
--
-- The failure was silent by construction: accounts.record_usage catches
-- PostgREST's rejection of unknown columns and logs it, so every analysis
-- succeeded and no token data was ever written. Verified 2026-09-07 against
-- the live database: 1 row, input_tokens set on 0, output_tokens set on 0.
-- Cost-per-analysis (ROADMAP 5.6) has therefore been collecting nothing, and
-- 6.1 pricing depends on it.
--
-- Rename rather than add: both existing columns are entirely NULL, so nothing
-- is lost, and the alternative leaves five token columns of which two are dead.
-- This supersedes 2026-08-21-usage-token-columns.sql — do not apply that one.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'analysis_usage' AND column_name = 'input_tokens')
    THEN
        ALTER TABLE analysis_usage RENAME COLUMN input_tokens TO prompt_tokens;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'analysis_usage' AND column_name = 'output_tokens')
    THEN
        ALTER TABLE analysis_usage RENAME COLUMN output_tokens TO response_tokens;
    END IF;
END $$;

ALTER TABLE analysis_usage
    ADD COLUMN IF NOT EXISTS prompt_tokens   integer,
    ADD COLUMN IF NOT EXISTS response_tokens integer,
    ADD COLUMN IF NOT EXISTS total_tokens    integer;

-- Nullable on purpose. Rows written before this have no token counts and never
-- will; NOT NULL DEFAULT 0 would make them indistinguishable from analyses that
-- genuinely cost nothing, quietly dragging the average cost down.

CREATE INDEX IF NOT EXISTS idx_analysis_usage_created ON analysis_usage (created_at);

-- ─────────────────────────────────────────────────────────────────────────
-- PART 2 — profile columns for the beta
-- ─────────────────────────────────────────────────────────────────────────

ALTER TABLE profiles
    -- B.10. Assigned at signup; the unique index below is the real guarantee.
    ADD COLUMN IF NOT EXISTS referral_code text,
    ADD COLUMN IF NOT EXISTS referred_by   uuid REFERENCES profiles (id) ON DELETE SET NULL,
    -- B.19. NULL means "never chose" — distinct from choosing the default, and
    -- that distinction is the whole preference signal the A/B toggle collects.
    ADD COLUMN IF NOT EXISTS ui_mode text
        CHECK (ui_mode IS NULL OR ui_mode IN ('TERMINAL', 'CONVENTIONAL')),
    -- B.9. Soft delete only. The 14-day sweeper for unverified accounts must
    -- never hard-delete a user: analysis_reports is what the accuracy record is
    -- built from, and a cascade would silently shrink it.
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz,
    ADD COLUMN IF NOT EXISTS verification_reminder_sent_at timestamptz;

CREATE UNIQUE INDEX IF NOT EXISTS idx_profiles_referral_code
    ON profiles (referral_code) WHERE referral_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_profiles_referred_by
    ON profiles (referred_by) WHERE referred_by IS NOT NULL;

-- Partial: the sweeper only ever scans live accounts.
CREATE INDEX IF NOT EXISTS idx_profiles_live ON profiles (created_at)
    WHERE deleted_at IS NULL;

-- ─────────────────────────────────────────────────────────────────────────
-- PART 3 — B.15, the waitlist
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS waitlist (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email         text        NOT NULL,
    referral_code text,
    source        text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    invited_at    timestamptz
);

COMMENT ON TABLE waitlist IS
    'Pre-signup email capture from the landing page. Converting an entry to an invite is a flag on profiles, not a move between tables.';

-- Case-insensitive: Foo@x.com and foo@x.com are one person, and a duplicate
-- would send them the invite twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_waitlist_email_lower ON waitlist (lower(email));
CREATE INDEX IF NOT EXISTS idx_waitlist_uninvited ON waitlist (created_at)
    WHERE invited_at IS NULL;

-- ─────────────────────────────────────────────────────────────────────────
-- PART 4 — B.10, referrals
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS referrals (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    referrer_profile_id uuid NOT NULL REFERENCES profiles (id) ON DELETE CASCADE,
    referred_profile_id uuid NOT NULL REFERENCES profiles (id) ON DELETE CASCADE,
    converted_at        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    -- A person can be referred once. Without this, re-signing-up farms rewards.
    CONSTRAINT referrals_referred_once UNIQUE (referred_profile_id),
    -- Self-referral is the other obvious exploit.
    CONSTRAINT referrals_no_self CHECK (referrer_profile_id <> referred_profile_id)
);

COMMENT ON TABLE referrals IS
    'Only rows with converted_at set count toward a reward — conversion means the referred user verified their email, so an unverified signup earns nothing.';

CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals (referrer_profile_id);
CREATE INDEX IF NOT EXISTS idx_referrals_converted
    ON referrals (referrer_profile_id) WHERE converted_at IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────
-- PART 5 — B.19, product telemetry
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_events (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_id uuid REFERENCES profiles (id) ON DELETE CASCADE,
    name       text NOT NULL,
    ui_mode    text CHECK (ui_mode IS NULL OR ui_mode IN ('TERMINAL', 'CONVENTIONAL')),
    properties jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE user_events IS
    'Product events only — UI mode switches, analyses started, quota-wall clicks. Never PII beyond profile_id. profile_id is nullable so pre-signup landing events can be recorded.';

CREATE INDEX IF NOT EXISTS idx_user_events_name_created ON user_events (name, created_at);
CREATE INDEX IF NOT EXISTS idx_user_events_profile ON user_events (profile_id, created_at);

-- ─────────────────────────────────────────────────────────────────────────
-- PART 6 — B.6, share tokens
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS share_tokens (
    token             text PRIMARY KEY,
    report_id         bigint NOT NULL REFERENCES analysis_reports (id) ON DELETE CASCADE,
    created_by        uuid REFERENCES profiles (id) ON DELETE SET NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    expires_at        timestamptz,
    revoked_at        timestamptz
);

COMMENT ON TABLE share_tokens IS
    'A deliberately minted public link for one report (UI_PRD 6.7). This is why GET /v1/analyses/{id} is not simply authenticated: sharing is an explicit act, not an accident of an unguessable id.';

CREATE INDEX IF NOT EXISTS idx_share_tokens_report ON share_tokens (report_id);
CREATE INDEX IF NOT EXISTS idx_share_tokens_live ON share_tokens (token)
    WHERE revoked_at IS NULL;

-- ─────────────────────────────────────────────────────────────────────────
-- PART 7 — RLS: deny by default
-- ─────────────────────────────────────────────────────────────────────────
--
-- Cygnus reaches Postgres with the service-role key, which bypasses RLS, so
-- these policies do not affect it. They exist so that if the anon key is ever
-- exposed — and it is a publishable key, so assume it will be — none of this
-- is readable. Enabled with no policies means deny-all for anon and authed.
-- Matches the posture already set on the existing tables.

ALTER TABLE waitlist     ENABLE ROW LEVEL SECURITY;
ALTER TABLE referrals    ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_events  ENABLE ROW LEVEL SECURITY;
ALTER TABLE share_tokens ENABLE ROW LEVEL SECURITY;

-- After applying, confirm token capture is actually working — the thing that
-- silently failed before:
--
--   select count(*) filter (where total_tokens is not null) as with_tokens,
--          count(*) as total
--     from analysis_usage where created_at > now() - interval '1 day';
--
-- Zero with_tokens after a generation cycle means the write is still failing.
-- Check the Cygnus logs for "failed to record usage". Silence is not success.
