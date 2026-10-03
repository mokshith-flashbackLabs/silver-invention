-- Reverses 0046. DELIBERATELY DESTRUCTIVE: every run's step log goes with the table (its
-- grant with it). The runs themselves, their outcomes and their metering are untouched.
-- COORDINATED: roll the backend back first -- it relays GET /runs/{run_id}/events.

DROP TABLE intel_run_events;
DROP INDEX IF EXISTS intel_runs_question_idx;
DROP INDEX IF EXISTS intel_runs_kind_created_idx;
