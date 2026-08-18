-- Requested by the backend team, 2026-08-18: grant the proxy's MIGRATOR role
-- membership in `imageshield_proxy_ro`.
--
-- WHY THEY NEED IT. Their migrations create views that JOIN against ours --
-- `v_covered_persons` selects from `svc.v_person_enrolment_state` (CLAUDE.md §3:
-- "the proxy's own views JOIN against ours and you cannot JOIN against HTTP").
-- Postgres requires the role creating a view to hold SELECT on every underlying
-- relation AT CREATION TIME. 0017 granted membership to their runtime roles
-- (`app_backend`, `app_worker`) but not to `migrator_backend`, so their migration
-- fails at CREATE VIEW with `permission denied for view
-- v_person_enrolment_state` even though the finished view would be readable.
--
-- WHY IT DOES NOT WIDEN THE BOUNDARY. `imageshield_proxy_ro` holds USAGE on
-- `svc` and SELECT on the four contract views. Nothing else -- no grant on any
-- base table, no USAGE on `public` (0016). So this gives their migrator exactly
-- the read surface their application roles already have, and CLAUDE.md §3's rule
-- ("all user-facing reads through the four `svc` views and nothing else") is
-- unchanged. A view's base-table reads are checked against the VIEW OWNER, so
-- their migrator still cannot reach `public.enrolments` through them.
--
-- WHY THIS IS A NEW MIGRATION AND NOT AN EDIT TO 0017. 0017 is applied and its
-- checksum is recorded in `schema_migrations`; editing an applied migration is a
-- deploy-blocking error by design (scripts/migrate.py). A separate migration is
-- also the honest history -- the requirement was discovered after 0017 shipped.
--
-- WHY IT IS OURS TO RUN. Membership requires ADMIN OPTION on the role, and
-- `imageshield_proxy_ro` was created by this repo's runner in 0016, so only this
-- runner holds it. Their migrator cannot grant this to itself.
--
-- Conditional on the role existing, like 0017: `migrator_backend` is created by
-- the cluster bootstrap, not by either repo's migrations, and does not exist in
-- CI or in docker compose. Absent -> NOTICE and skip, so this is a successful
-- no-op there.

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migrator_backend') THEN
    -- Idempotent: re-granting an existing membership is not an error.
    GRANT imageshield_proxy_ro TO migrator_backend;
    RAISE NOTICE 'granted imageshield_proxy_ro to migrator_backend';
  ELSE
    RAISE NOTICE 'role migrator_backend absent; imageshield_proxy_ro not granted';
  END IF;
END
$$;
