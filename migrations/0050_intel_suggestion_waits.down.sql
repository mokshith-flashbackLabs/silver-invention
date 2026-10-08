-- Reverses 0050. A suggestion waiting when this runs keeps its 'queued' status and is claimed as
-- an ordinary run, answering from what it holds. Its 'waiting' log rows go (the rest of each log
-- stays), because 0046's vocabulary does not admit them.
DELETE FROM intel_run_events WHERE kind = 'waiting';
ALTER TABLE intel_run_events DROP CONSTRAINT intel_run_events_kind_valid;
ALTER TABLE intel_run_events ADD CONSTRAINT intel_run_events_kind_valid
  CHECK (kind IN ('run_started', 'model_call_started', 'thinking', 'search',
                  'search_results', 'writing', 'continuing', 'model_call_finished',
                  'model_call_failed', 'model_call_skipped', 'run_finished',
                  'truncated'));

ALTER TABLE intel_runs
  DROP CONSTRAINT intel_runs_wait_only_suggestions,
  DROP CONSTRAINT intel_runs_wait_paired,
  DROP COLUMN not_before,
  DROP COLUMN wait_deadline,
  DROP COLUMN awaiting_source_ids;
