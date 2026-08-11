-- Reverses 0010.
--
-- Dropping these columns loses the consent evidence for every enrolment, which
-- re-opens the gap this migration closed: with the columns gone, nothing
-- prevents a consent-free enrolment again. Reversibility is a CI requirement
-- (build spec Phase 1 §5) so the pair exists, but reverting this in an
-- environment holding real enrolments destroys the only record in this repo
-- that consent was collected at all. The proxy still holds the documents; the
-- binding from a face vector to the consent that authorised it is what is lost,
-- and re-applying `up` cannot rebuild it -- the backfill marks every surviving
-- row PRE_CONSENT_TEST_DATA. Recovery is a proxy-driven re-assertion of
-- consent_ref per enrolment.
--
-- Recorded here rather than in a runbook because this file is what an operator
-- reads immediately before running it.
--
-- DROP COLUMN takes enrolments_consent_not_sentinel and enrolments_consent_idx
-- with it; naming them here would only add a second way to fail.

ALTER TABLE enrolments
  DROP COLUMN consent_ref,
  DROP COLUMN consent_document_sha256,
  DROP COLUMN consent_signed_at;
