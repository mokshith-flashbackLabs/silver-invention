-- Reverses 0055. DELETES rather than refuses (0053/0054 precedent): every DB test fixture migrates
-- the shared database down, and a rollback must need no hand-deletion. The lesson proposals and
-- their evidence links go; the signals and the events stay. A queued lesson_draft run would
-- otherwise run after the rollback and spend budget, so lesson_draft runs go too.
DROP VIEW IF EXISTS svc.v_intel_weekly_totals;
DROP VIEW IF EXISTS svc.v_lessons;
DROP VIEW IF EXISTS svc.v_evidence;
DROP TABLE intel_published_evidence;
DROP TABLE intel_published_owners;
DROP TABLE intel_lessons;
DELETE FROM intel_proposal_signals
 WHERE proposal_id IN (SELECT proposal_id FROM intel_proposals WHERE kind = 'lesson');
DELETE FROM intel_proposals WHERE kind = 'lesson';
DELETE FROM intel_run_events
 WHERE run_id IN (SELECT run_id FROM intel_runs WHERE kind = 'lesson_draft');
-- The spend of their model calls stays; only the link to the run goes (a call may name no run).
UPDATE provider_calls SET intel_run_id = NULL
 WHERE intel_run_id IN (SELECT run_id FROM intel_runs WHERE kind = 'lesson_draft');
DELETE FROM intel_runs WHERE kind = 'lesson_draft';
ALTER TABLE intel_runs DROP CONSTRAINT intel_runs_kind_valid;
ALTER TABLE intel_runs ADD CONSTRAINT intel_runs_kind_valid
  CHECK (kind IN ('source_check', 'discovery', 'adhoc_url', 'weight_suggestion', 'renewal_check',
                  'gap_regenerate', 'source_proposal', 'source_validation'));
ALTER TABLE intel_proposals DROP CONSTRAINT intel_proposals_decided_check;
ALTER TABLE intel_proposals DROP CONSTRAINT intel_proposals_kind_check;
ALTER TABLE intel_proposals ADD CONSTRAINT intel_proposals_kind_check
  CHECK (kind IN ('weight_change', 'threat_event', 'protection_event', 'weight_suggestion',
                  'coverage_gap', 'quiz_question'));
ALTER TABLE intel_proposals ADD CONSTRAINT intel_proposals_decided_check
  CHECK (NOT (status IN ('approved', 'applied')
              AND kind IN ('weight_change', 'threat_event', 'protection_event', 'quiz_question'))
         OR decided IS NOT NULL);
