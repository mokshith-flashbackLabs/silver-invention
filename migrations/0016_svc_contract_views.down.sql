-- Reverses 0016.
--
-- Dropping the views BREAKS THE PROXY, not this service: nothing here reads
-- them. That asymmetry is the versioned-contract cost made concrete, and it is
-- why running this against a shared database is a coordinated deploy and not a
-- rollback somebody does alone.
--
-- Grants must be revoked before a role can be dropped, and a role cannot be
-- dropped while any database on the cluster still grants it anything -- the
-- same constraint 0001 and 0015 document. On a developer machine with more than
-- one database migrated, the DROP is skipped and the role legitimately
-- survives; that is not a failure.

REVOKE ALL ON ALL TABLES IN SCHEMA svc FROM imageshield_proxy_ro;
REVOKE USAGE ON SCHEMA svc FROM imageshield_proxy_ro;

DROP VIEW IF EXISTS svc.v_person_liveness_attempts;
DROP VIEW IF EXISTS svc.v_person_hits;
DROP VIEW IF EXISTS svc.v_person_report_summary;
DROP VIEW IF EXISTS svc.v_person_enrolment_state;

DROP SCHEMA IF EXISTS svc;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'imageshield_proxy_ro') THEN
    BEGIN
      DROP ROLE imageshield_proxy_ro;
    EXCEPTION WHEN dependent_objects_still_exist THEN
      RAISE NOTICE 'role imageshield_proxy_ro still holds grants in another '
                   'database; left in place';
    END;
  END IF;
END
$$;

ALTER TABLE infringements DROP CONSTRAINT infringements_status_valid;

-- Back to the three-signal vocabulary, NOT VALID -- the same shape 0010 used,
-- and for the same reason. The alternative was rejected twice over: a validating
-- constraint fails outright on any 'authorised' row already written, which makes
-- this migration non-reversible the moment the feature is used (a CI
-- requirement, build spec Phase 1 §5), and the only way to make it succeed is
-- DELETEing a user's recorded position on their own report without telling
-- anyone. NOT VALID keeps every statement anyone made and still refuses the
-- fourth signal from here on, which is what reverting the feature means.
--
-- `infringements.status` values of 'authorised' also survive, deliberately, on
-- 0012's reasoning: the status is the user's current position on a hit, and
-- resetting it would re-surface a match they have already resolved.
ALTER TABLE infringement_feedback
  DROP CONSTRAINT infringement_feedback_signal_check;

ALTER TABLE infringement_feedback
  ADD CONSTRAINT infringement_feedback_signal_check
  CHECK (signal IN ('not_me', 'confirmed', 'uncertain')) NOT VALID;
