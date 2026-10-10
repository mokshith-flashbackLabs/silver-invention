-- Reverses 0053. Refuses while any research source exists: deleting one would orphan the
-- documents and signals its read produced, and 0052's origin vocabulary does not admit it.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM intel_sources WHERE origin = 'research') THEN
    RAISE EXCEPTION 'research sources exist: delete them and their evidence before reversing 0053';
  END IF;
END
$$;
DELETE FROM intel_run_events WHERE kind = 'research_round';
ALTER TABLE intel_run_events DROP CONSTRAINT intel_run_events_kind_valid;
ALTER TABLE intel_run_events ADD CONSTRAINT intel_run_events_kind_valid
  CHECK (kind IN ('run_started', 'model_call_started', 'thinking', 'search',
                  'search_results', 'writing', 'continuing', 'model_call_finished',
                  'model_call_failed', 'model_call_skipped', 'run_finished',
                  'truncated', 'waiting'));
ALTER TABLE intel_runs
  DROP CONSTRAINT intel_runs_research_round_non_negative,
  DROP CONSTRAINT intel_runs_research_only_suggestions,
  DROP COLUMN research_pass,
  DROP COLUMN research_source_ids,
  DROP COLUMN research_round;
ALTER TABLE intel_sources DROP CONSTRAINT intel_sources_disabled_reason_valid;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_disabled_reason_valid
  CHECK (disabled_reason IN ('too_short', 'unreachable', 'unmapped'));
ALTER TABLE intel_sources DROP CONSTRAINT intel_sources_research_shape;
ALTER TABLE intel_sources DROP CONSTRAINT intel_sources_origin_valid;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_origin_valid
  CHECK (origin IN ('suggested', 'operator', 'news_watch'));
