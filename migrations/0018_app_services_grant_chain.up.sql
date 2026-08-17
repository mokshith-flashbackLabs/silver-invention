-- Deploy coordination: complete OUR grant chain, the way 0017 completed the
-- proxy's.
--
-- WHAT WAS WRONG. 0015 creates four NOLOGIN module roles -- identity_rw,
-- search_rw, calibration_rw, audit_w -- and gives each exactly the table grants
-- its module needs. 0001 does the same for `imageshield_app`. All five are grant
-- targets rather than identities, and NOTHING HAS EVER BEEN GRANTED MEMBERSHIP
-- IN ANY OF THEM. So the least-privilege model 0015 describes applies to nobody,
-- and the role the service actually connects as holds nothing.
--
-- Until the dev deploy this was invisible for the same reason 0017's gap was:
-- every test connects as the role that ran the migrations, which owns the tables
-- and can therefore read everything. Ownership and a correct grant chain look
-- identical from there. In dev the service connects as `app_services`, which
-- inherits nothing from ownership -- `DEPLOY-DEV.md` §4 records that the cluster
-- bootstrap deliberately applies NO table grants ("those belong in each repo's
-- migrations") and additionally runs
-- `REVOKE ALL ON SCHEMA public FROM PUBLIC`. So without this migration
-- `app_services` cannot USAGE the schema, let alone read a table: every request
-- fails with `permission denied`, on a container that started cleanly and whose
-- /readyz reports ready, because /readyz reads pg_catalog precisely so that a
-- missing grant cannot masquerade as a missing view.
--
-- WHY MEMBERSHIP AND NOT DIRECT GRANTS. Granting the tables to `app_services`
-- directly would duplicate 0015's enumeration and let the two drift -- a new
-- table added to search_rw in a later migration would silently not reach the
-- service. Membership means 0015 stays the single place that decides which
-- module owns which table, which is what its own comment says the enumeration is
-- for.
--
-- WHY audit_w IS INCLUDED. `audit_log` is INSERT-only by design (0015, and 0001
-- before it): an audit log the application can edit is not an audit log. Joining
-- audit_w grants INSERT and nothing else, so that property survives -- the
-- service can write an audit row and cannot amend one. tests/test_migrations.py
-- already asserts UPDATE and DELETE raise InsufficientPrivilege.
--
-- WHY THE ROLE NAME IS CONDITIONAL. `app_services` is created by the cluster
-- bootstrap (`DEPLOY-DEV.md` §4), not by this repo, and it does not exist in CI
-- or in docker compose. A name that is absent raises a NOTICE and is skipped,
-- so this migration is a successful no-op wherever the deploy role does not
-- exist -- the same shape 0017 uses, for the same reason.
--
-- WHAT THIS DELIBERATELY DOES NOT DO. It does not CREATE `app_services`. That
-- role belongs to the cluster bootstrap, and a migration here that conjured it
-- would make a missing deploy step look like a successful one. It also does not
-- touch `schema_migrations`: 0015's closing note is that no application role
-- gets anything on the ledger, because a role that could edit it could make an
-- applied migration look unapplied. Membership in the module roles grants
-- nothing there, and that is intentional.

DO $$
DECLARE
  login_role TEXT := 'app_services';
  module_role TEXT;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = login_role) THEN
    RAISE NOTICE 'role % absent; module roles not granted to it', login_role;
    RETURN;
  END IF;

  FOREACH module_role IN ARRAY
    ARRAY['identity_rw', 'search_rw', 'calibration_rw', 'audit_w', 'imageshield_app']
  LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = module_role) THEN
      -- Idempotent: re-granting an existing membership is not an error.
      EXECUTE format('GRANT %I TO %I', module_role, login_role);
      RAISE NOTICE 'granted % to %', module_role, login_role;
    ELSE
      RAISE NOTICE 'module role % absent; skipped', module_role;
    END IF;
  END LOOP;
END
$$;
