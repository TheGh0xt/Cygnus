-- ROADMAP B.11 — explanation calibration.
--
-- Apply to the Supabase Postgres (SQL editor or CLI). Until it is applied,
-- Cygnus keeps working: the code reads this column with .get() and falls
-- back to confidence_score when it is absent, so nothing 500s. Calibration
-- just bins on the wrong value in the meantime — see below.
--
-- Why this column exists: analysis_reports.confidence_score is deliberately
-- mutable. The T+48h evaluation worker overwrites it (record_checkpoint's
-- canonical horizon) as the self-correction loop's "current best estimate",
-- and that is the value every other reader — the API, a market's history —
-- is supposed to see.
--
-- Calibration needs the opposite: what the analyst originally claimed,
-- *before* any outcome fed back into it. Binning a scored report by its own
-- post-hoc-adjusted confidence would measure how much the adjustment moved
-- the score, not how well-calibrated the original claim was — a correct call
-- gets nudged toward "correct" by the very adjustment that scored it. That
-- is not a hypothetical: every canonically-evaluated row already has this
-- problem, because record_checkpoint has always overwritten confidence_score
-- in place.
ALTER TABLE analysis_reports
    ADD COLUMN IF NOT EXISTS stated_confidence_score DOUBLE PRECISION;

-- Backfill: for a report that has never been evaluated, confidence_score
-- *is* still the stated value (nothing has overwritten it yet), so this is
-- exact for the common case. For a report already canonically evaluated
-- before this migration runs, confidence_score has already been adjusted
-- and the true original claim is unrecoverable — this backfills the
-- adjusted value as a best-effort default, which is wrong for exactly those
-- rows. Acceptable here only because production has evaluated a handful of
-- reports total as of this migration (see PROJECT_STATUS.md) and the
-- calibration endpoint is gated behind n>300 regardless, so this backfill
-- inaccuracy cannot reach a published curve.
UPDATE analysis_reports
SET stated_confidence_score = confidence_score
WHERE stated_confidence_score IS NULL;
