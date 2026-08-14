-- Layer 3 memory store schema.
-- Kept ANSI-portable: this DDL seeds the SQLite MVP today and the planned
-- pgvector-backed store later (vector columns will be added there).
CREATE TABLE IF NOT EXISTS analysis_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    report_json TEXT NOT NULL,          -- full MarketAnalysisReport JSON
    confidence_score REAL NOT NULL,     -- denormalized, updated by evaluation
    -- Nullable: if the price fetch fails at report time we still persist the
    -- report rather than losing it. The evaluation worker skips rows with a
    -- null price, which is recoverable; a dropped report never is.
    price_at_report REAL,
    created_at TEXT NOT NULL,           -- ISO-8601 UTC
    evaluated_at TEXT,
    outcome TEXT                        -- CONFIRMED / REVERSED / NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_market ON analysis_reports (market_id);
CREATE INDEX IF NOT EXISTS idx_reports_pending
    ON analysis_reports (created_at) WHERE evaluated_at IS NULL;

-- Escalating evaluation horizons: one row per report per checkpoint.
--
-- A single check at T+48h answers "was it right" and nothing else. Checking
-- at 12, 18, 24 and 48 hours answers how durable the explanation was: "held
-- at 12h, held at 18h, reversed by 48h" and "wrong from the start" are
-- different results a single checkpoint records identically.
--
-- The 48h row is canonical. It alone adjusts the report's confidence and is
-- the horizon the published accuracy record is computed from. Earlier rows
-- are observations, not score changes: the confidence matrix was designed to
-- apply once, and running it four times would swing scores four times as hard
-- and let a wobbling report whipsaw itself.
CREATE TABLE IF NOT EXISTS report_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES analysis_reports (id) ON DELETE CASCADE,
    horizon_hours INTEGER NOT NULL,
    observed_price REAL NOT NULL,
    outcome TEXT NOT NULL,              -- CONFIRMED / REVERSED
    is_canonical INTEGER NOT NULL DEFAULT 0,
    evaluated_at TEXT NOT NULL,         -- ISO-8601 UTC
    -- Recorded once per horizon. This is what makes a cycle idempotent: a
    -- re-run finds the row present and skips it.
    UNIQUE (report_id, horizon_hours)
);
CREATE INDEX IF NOT EXISTS idx_report_evaluations_report
    ON report_evaluations (report_id, horizon_hours);
