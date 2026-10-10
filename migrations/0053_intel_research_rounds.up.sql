-- Every answer is researched (spec 2026-10-10-intel-every-answer-researched §2.2, §3). A research
-- search is a saved search a weight suggestion registers for an answer it could not back: read
-- once by its own discovery run and never scheduled (enabled = false, disabled_reason
-- 'research_once'; queue_source_reads reads a named source whatever enabled says), proposed_for
-- its question and answer.
ALTER TABLE intel_sources DROP CONSTRAINT intel_sources_origin_valid;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_origin_valid
  CHECK (origin IN ('suggested', 'operator', 'news_watch', 'research'));
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_research_shape
  CHECK (origin <> 'research' OR (kind = 'search_query' AND proposed_for IS NOT NULL));
ALTER TABLE intel_sources DROP CONSTRAINT intel_sources_disabled_reason_valid;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_disabled_reason_valid
  CHECK (disabled_reason IN ('too_short', 'unreachable', 'unmapped', 'research_once'));

-- A weight suggestion's research state across its waits: the round in progress, the research
-- searches it registered (retrieval reads them beside request.source_ids), and the pass that
-- started the current round (written if a later round cannot finish).
ALTER TABLE intel_runs
  ADD COLUMN research_round SMALLINT NOT NULL DEFAULT 0,
  ADD COLUMN research_source_ids UUID[],
  ADD COLUMN research_pass JSONB,
  ADD CONSTRAINT intel_runs_research_only_suggestions
    CHECK (kind = 'weight_suggestion'
           OR (research_round = 0 AND research_source_ids IS NULL AND research_pass IS NULL)),
  ADD CONSTRAINT intel_runs_research_round_non_negative CHECK (research_round >= 0);

-- The run log's one new step: "Searching again for 2 answers (round 1 of 4)".
ALTER TABLE intel_run_events DROP CONSTRAINT intel_run_events_kind_valid;
ALTER TABLE intel_run_events ADD CONSTRAINT intel_run_events_kind_valid
  CHECK (kind IN ('run_started', 'model_call_started', 'thinking', 'search',
                  'search_results', 'writing', 'continuing', 'model_call_finished',
                  'model_call_failed', 'model_call_skipped', 'run_finished',
                  'truncated', 'waiting', 'research_round'));
