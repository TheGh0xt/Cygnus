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
