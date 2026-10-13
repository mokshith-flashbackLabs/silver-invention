-- Reverses 0054. The quiz_question proposals go: what they proposed lives on in the backend's
-- published quiz, which this does not touch, and their audit rows stay. Deleted rather than
-- refused, so a rollback needs no hand-deletion and the test suite can migrate every database
-- down between tests (0053's precedent). Their evidence links go with them; the signals stay.
DELETE FROM intel_proposal_signals
 WHERE proposal_id IN (SELECT proposal_id FROM intel_proposals WHERE kind = 'quiz_question');
DELETE FROM intel_proposals WHERE kind = 'quiz_question';
ALTER TABLE intel_proposals DROP CONSTRAINT intel_proposals_decided_check;
ALTER TABLE intel_proposals DROP CONSTRAINT intel_proposals_kind_check;
ALTER TABLE intel_proposals ADD CONSTRAINT intel_proposals_kind_check
  CHECK (kind IN ('weight_change', 'threat_event', 'protection_event', 'weight_suggestion',
                  'coverage_gap'));
ALTER TABLE intel_proposals ADD CONSTRAINT intel_proposals_decided_check
  CHECK (NOT (status IN ('approved', 'applied')
              AND kind IN ('weight_change', 'threat_event', 'protection_event'))
         OR decided IS NOT NULL);
