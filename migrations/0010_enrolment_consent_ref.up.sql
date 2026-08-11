-- The consent reference on `enrolments`, and the second half of INVARIANTS #2.
--
-- #2 has always read "no enrolment without a passed liveness session AND a
-- signed consent record". The first half is enforced by 0003's composite FK.
-- The second half was enforced NOWHERE -- there is no consent table in this
-- repo -- so a consent-free enrolment was writable. For Article 9 biometric
-- processing that is the wrong state to be in.
--
-- The original design put `consent_records` here. That is reversed: the PROXY
-- owns consent. It has profile.persons, profile.guardianships with subject_dob
-- triggers, and profile.v_consent_eligibility computing required_signer_role --
-- the hard part, already built. It is also the only public ingress, so the
-- DocuSeal webhook must terminate there. This repo knows a user_ref and a face
-- vector; it cannot determine who is required to sign.
--
-- So we hold a REFERENCE, not a record. Enforcement survives; the document does
-- not cross the boundary. We store a hash the proxy computed and never compute
-- one ourselves -- we never see the document.

ALTER TABLE enrolments
  ADD COLUMN consent_ref             UUID,
  ADD COLUMN consent_document_sha256 TEXT,
  ADD COLUMN consent_signed_at       TIMESTAMPTZ;

-- Rows created before consent was required. Backfill with a sentinel so NOT
-- NULL can be applied without deleting them -- CLAUDE.md §5 says never DELETE,
-- and a biometric enrolment is the single record that the face was ever
-- indexed. This UUID is reserved and the proxy must never issue it.
UPDATE enrolments
   SET consent_ref             = '00000000-0000-0000-0000-000000000000',
       consent_document_sha256 = 'PRE_CONSENT_TEST_DATA',
       consent_signed_at       = created_at
 WHERE consent_ref IS NULL;

ALTER TABLE enrolments
  ALTER COLUMN consent_ref             SET NOT NULL,
  ALTER COLUMN consent_document_sha256 SET NOT NULL,
  ALTER COLUMN consent_signed_at       SET NOT NULL;

-- The sentinel is a migration artifact, not a valid state going forward: a
-- FRESH enrolment carrying it must fail at the database, not in application
-- code.
--
-- NOT VALID, rather than the date-literal form the task brief sketched
-- (`consent_ref <> sentinel OR created_at < '<migration date>'`). NOT VALID is
-- Postgres's exact expression of "grandfather what is already here, enforce on
-- every future INSERT and UPDATE", and it is strictly stronger than the date:
--
--   * The date form aborts THIS migration on any row created earlier the same
--     day -- those rows get the sentinel and then fail the CHECK being added.
--   * It re-aborts on every subsequent down/up cycle, because `up` re-backfills
--     rows whose created_at is now past the literal. CI runs up/down/up.
--   * It leaves a window on the migration date itself in which a genuinely new
--     enrolment could carry the sentinel and be accepted.
--
-- NOT VALID has none of those and needs no calendar literal to go stale. The
-- grandfathered rows stay identifiable by consent_document_sha256.
ALTER TABLE enrolments
  ADD CONSTRAINT enrolments_consent_not_sentinel
  CHECK (consent_ref <> '00000000-0000-0000-0000-000000000000') NOT VALID;

-- "Which enrolments does this consent record cover" is the reconciliation
-- question the proxy asks after a withdrawal.
CREATE INDEX enrolments_consent_idx ON enrolments (consent_ref);
