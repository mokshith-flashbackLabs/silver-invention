-- Reverses 0038. Rolling the feature back throws away its metering ON PURPOSE —
-- downs run in dev and CI (0004's down carries the same note).

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_services') THEN
    REVOKE intel_rw FROM app_services;
  END IF;
END
$$;

ALTER TABLE providers DROP CONSTRAINT providers_llm_never_calibrated;
ALTER TABLE provider_calls DROP CONSTRAINT provider_calls_one_run;
DELETE FROM provider_calls WHERE provider_id = 'claude_intel' OR intel_run_id IS NOT NULL;
ALTER TABLE provider_calls DROP COLUMN intel_run_id;
DELETE FROM provider_spend WHERE provider_id = 'claude_intel';
DELETE FROM providers WHERE provider_id = 'claude_intel';

DROP TABLE intel_proposal_signals;
DROP TABLE intel_proposals;
DROP TABLE intel_excerpts;
DROP TABLE intel_signals;
DROP TABLE intel_documents;
DROP TABLE intel_vocabulary;
DROP TABLE intel_snapshots;
DROP TABLE intel_runs;
DROP TABLE intel_sources;
DROP FUNCTION intel_tags_well_formed(text[]);

REVOKE SELECT (url_hash) ON content_urls FROM intel_rw;
REVOKE USAGE ON SEQUENCE audit_log_audit_id_seq FROM intel_rw;
REVOKE INSERT ON audit_log FROM intel_rw;
REVOKE SELECT, INSERT, UPDATE ON provider_calls, provider_spend FROM intel_rw;
REVOKE SELECT, UPDATE ON providers FROM intel_rw;

ALTER TABLE providers DROP CONSTRAINT providers_kind_valid;
CREATE TYPE provider_kind AS ENUM ('image_search', 'face_search', 'classifier');
ALTER TABLE providers ALTER COLUMN kind TYPE provider_kind USING kind::provider_kind;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'intel_rw') THEN
    REVOKE USAGE ON SCHEMA public FROM intel_rw;
    BEGIN
      DROP ROLE intel_rw;
    EXCEPTION WHEN dependent_objects_still_exist THEN
      RAISE NOTICE 'role intel_rw still holds grants in another database; left in place';
    END;
  END IF;
END
$$;
