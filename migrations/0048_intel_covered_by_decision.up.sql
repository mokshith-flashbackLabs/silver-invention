-- 0048 -- likeness intel: an approval supersedes the pending proposals it overlaps (spec
-- docs/superpowers/specs/2026-10-04-intel-evidence-quality-design.md section 5).
--
-- The same body of evidence used to produce two kinds of proposal -- a lasting weight_change and
-- a temporary threat_event on the same platform, or one fact framed as both a protection and a
-- risk. Both are still written, and both reads name the other under `overlaps`; when an operator
-- APPROVES one, every pending proposal it overlaps is superseded in the decision's transaction
-- with this new reason. Rejecting one supersedes nothing.
--
-- 0039 wrote the supersede_reason CHECK unnamed, so it is found by its definition, never by an
-- assumed name (0043's and 0047's precedent). The status <-> reason pairing CHECK is untouched.
DO $$
DECLARE
  found text;
BEGIN
  FOR found IN
    SELECT conname FROM pg_constraint
     WHERE conrelid = 'intel_proposals'::regclass AND contype = 'c'
       AND pg_get_constraintdef(oid) LIKE '%supersede_reason%'
       AND pg_get_constraintdef(oid) LIKE '%resolved_by_quiz%'
  LOOP
    EXECUTE format('ALTER TABLE intel_proposals DROP CONSTRAINT %I', found);
  END LOOP;
END
$$;
ALTER TABLE intel_proposals ADD CONSTRAINT intel_proposals_supersede_reason_check
  CHECK (supersede_reason IN ('newer_proposal', 'cell_changed', 'resolved_by_quiz',
                              'covered_by_decision'));

-- No grant: 0039's table-level grants to intel_rw cover the column.
