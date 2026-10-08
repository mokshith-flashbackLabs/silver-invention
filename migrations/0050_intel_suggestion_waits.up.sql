-- A points suggestion waits for the reads it queued (spec
-- 2026-10-08-intel-suggestion-waits-for-evidence §2, §3). While it waits its run is 'queued' and
-- claimable only once none of awaiting_source_ids has a read open, or wait_deadline has passed,
-- and never before not_before. All three are null on every other run.
ALTER TABLE intel_runs
  ADD COLUMN awaiting_source_ids UUID[],
  ADD COLUMN wait_deadline TIMESTAMPTZ,
  ADD COLUMN not_before TIMESTAMPTZ,
  ADD CONSTRAINT intel_runs_wait_paired
    CHECK ((awaiting_source_ids IS NULL) = (wait_deadline IS NULL)),
  ADD CONSTRAINT intel_runs_wait_only_suggestions
    CHECK (awaiting_source_ids IS NULL OR kind = 'weight_suggestion');

-- The run log's one new step: "Waiting for 3 sources to be read before suggesting".
ALTER TABLE intel_run_events DROP CONSTRAINT intel_run_events_kind_valid;
ALTER TABLE intel_run_events ADD CONSTRAINT intel_run_events_kind_valid
  CHECK (kind IN ('run_started', 'model_call_started', 'thinking', 'search',
                  'search_results', 'writing', 'continuing', 'model_call_finished',
                  'model_call_failed', 'model_call_skipped', 'run_finished',
                  'truncated', 'waiting'));
