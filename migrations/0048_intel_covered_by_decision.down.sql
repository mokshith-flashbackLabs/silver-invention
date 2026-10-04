-- Reverses 0048.
--
-- 0039's three reasons come back. A row superseded covered_by_decision is relabelled
-- newer_proposal -- the nearest of the three: a later decision displaced it -- so no row is lost
-- and none fails the restored CHECK. Which decision covered it stays in that decision's
-- audit_log row (intel.proposal_decided, metadata.superseded).
UPDATE intel_proposals SET supersede_reason = 'newer_proposal'
 WHERE supersede_reason = 'covered_by_decision';
ALTER TABLE intel_proposals DROP CONSTRAINT intel_proposals_supersede_reason_check;
ALTER TABLE intel_proposals ADD CONSTRAINT intel_proposals_supersede_reason_check
  CHECK (supersede_reason IN ('newer_proposal', 'cell_changed', 'resolved_by_quiz'));
