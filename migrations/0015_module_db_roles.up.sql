-- Step 9: one database role per module, least privilege.
--
-- WHY THIS IS A MIGRATION AND NOT TERRAFORM. The step-9 brief says "defined in
-- IaC alongside the migrations that create the schemas". Postgres roles and
-- grants are not AWS resources; expressing them in Terraform means the
-- cloudflare/postgresql provider holding superuser credentials and connecting
-- to the database from wherever `terraform apply` runs -- a second, weaker path
-- into the same database, for objects the migration runner is already
-- authorised to create. Migration 0001 already creates `imageshield_app` this
-- way. Versioned, reversible, checksummed and run in CI is what "IaC" is
-- protecting; a .tf file is not the only shape that has those properties.
--
-- The grants are deliberately table-level and enumerated rather than
-- ALL TABLES IN SCHEMA. A future table joins no role by accident: somebody has
-- to decide which module owns it, in a migration, in review. That is the point.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'identity_rw') THEN
    CREATE ROLE identity_rw NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'search_rw') THEN
    CREATE ROLE search_rw NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'calibration_rw') THEN
    CREATE ROLE calibration_rw NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'audit_w') THEN
    CREATE ROLE audit_w NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO identity_rw, search_rw, calibration_rw, audit_w;

-- identity: the liveness/enrolment path, plus the subject eligibility flag it
-- writes in the same transaction as an enrolment.
GRANT SELECT, INSERT, UPDATE ON
  liveness_sessions, enrolments, subjects
  TO identity_rw;

-- search: discovery, its results, the provider control plane, and the two
-- tables the out-of-band tasks added -- infringement_feedback (03) and the
-- attribution pair (05). Attribution writes search_seeds, so it belongs to this
-- role rather than identity's, despite reading the identity collection.
GRANT SELECT, INSERT, UPDATE ON
  search_seeds, search_runs, content_urls, infringements, attestations,
  provider_calls, providers, provider_spend, infringement_feedback,
  attribution_runs, attributed_faces
  TO search_rw;

GRANT SELECT, INSERT, UPDATE ON
  calibration_configs, eval_items, eval_observations
  TO calibration_rw;

-- INSERT ONLY. No UPDATE, no DELETE, and that shape is the whole point: an
-- audit log an application can edit is not an audit log. Migration 0001 already
-- proves this for imageshield_app (tests/test_migrations.py asserts UPDATE and
-- DELETE raise InsufficientPrivilege under it); this is the same grant for the
-- named role, so the property survives the move to per-module roles.
GRANT INSERT ON audit_log TO audit_w;
GRANT USAGE ON SEQUENCE audit_log_audit_id_seq TO audit_w;

-- Sequences for the tables each role inserts into. UUID primary keys need
-- none; audit_log's bigserial is the only one, and it is granted above.

-- No role is granted anything on `schema_migrations`. The migration runner
-- connects as the owner; an application role that could edit the ledger could
-- make an applied migration look unapplied.
