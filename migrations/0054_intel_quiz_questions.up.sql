-- 0054 -- likeness intel: the AI proposes quiz questions (spec
-- docs/superpowers/specs/2026-10-13-intel-quiz-questions-design.md section 8).
--
-- A sixth proposal kind, quiz_question: a new question with its answers and their tags, researched
-- by an ordinary weight_suggestion run and decided by a named operator. The backend publishes the
-- approved ones as a new quiz version and acknowledges them, which moves them to 'applied'.
--
-- 0039 wrote both CHECKs this changes unnamed, so they are found by their definitions, never by an
-- assumed name (0048's precedent): the kind list, and "an approved or applied proposal of a kind
-- that applies something carries its decided values".
DO $$
DECLARE
  found text;
BEGIN
  FOR found IN
    SELECT conname FROM pg_constraint
     WHERE conrelid = 'intel_proposals'::regclass AND contype = 'c'
       AND (
         (pg_get_constraintdef(oid) LIKE '%coverage_gap%'
          AND pg_get_constraintdef(oid) LIKE '%weight_suggestion%'
          AND pg_get_constraintdef(oid) NOT LIKE '%status%')
         OR (pg_get_constraintdef(oid) LIKE '%decided IS NOT NULL%'
             AND pg_get_constraintdef(oid) LIKE '%protection_event%')
       )
  LOOP
    EXECUTE format('ALTER TABLE intel_proposals DROP CONSTRAINT %I', found);
  END LOOP;
END
$$;
ALTER TABLE intel_proposals ADD CONSTRAINT intel_proposals_kind_check
  CHECK (kind IN ('weight_change', 'threat_event', 'protection_event', 'weight_suggestion',
                  'coverage_gap', 'quiz_question'));
ALTER TABLE intel_proposals ADD CONSTRAINT intel_proposals_decided_check
  CHECK (NOT (status IN ('approved', 'applied')
              AND kind IN ('weight_change', 'threat_event', 'protection_event', 'quiz_question'))
         OR decided IS NOT NULL);

-- No grant: 0039's table-level grants to intel_rw cover the table.
