-- Reverse of 0001_initial_schema.up.sql. Revoke the application-role grants
-- and attempt to drop the role first, then drop tables in reverse
-- dependency order, then the enum types.
--
-- Roles are cluster-global, not per-database: this migration may run
-- against a database that shares a Postgres server with another database
-- that also has 0001 applied (this repo's own test harness creates a fresh
-- throwaway database per session, and two pytest sessions against the same
-- server hit exactly this). REVOKE first, guarded on the role actually
-- existing (a prior down migration on another database on this server may
-- already have dropped it — REVOKE ... FROM a nonexistent role errors
-- otherwise). Then attempt DROP ROLE: it only succeeds once no database on
-- the cluster still grants the role anything. If another database's grants
-- are still live, Postgres raises dependent_objects_still_exist (SQLSTATE
-- 2BP01, "role ... cannot be dropped because some objects depend on it");
-- catch it and leave the role in place — this database's own grants are
-- already gone above, so nothing here is left dangling. Two down
-- migrations racing on two different databases on the same server each
-- revoke only their own grants safely; whichever runs its DROP ROLE last
-- (after both revokes have landed) is the one that actually removes it.

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'imageshield_app') THEN
    REVOKE USAGE ON SEQUENCE audit_log_audit_id_seq FROM imageshield_app;
    REVOKE INSERT ON audit_log FROM imageshield_app;
  END IF;
END
$$;

DO $$
BEGIN
  DROP ROLE IF EXISTS imageshield_app;
EXCEPTION
  WHEN dependent_objects_still_exist THEN
    RAISE NOTICE 'imageshield_app still referenced by another database on this server; leaving role in place';
END
$$;

-- ── Audit ─────────────────────────────────────────────────────────────────

DROP TABLE IF EXISTS audit_log;

-- ── Outbox ────────────────────────────────────────────────────────────────

DROP TABLE IF EXISTS outbox;

-- ── Providers and classification (reverse creation order) ─────────────────

DROP TABLE IF EXISTS search_matches;
DROP TABLE IF EXISTS provider_calls;
DROP TABLE IF EXISTS content_urls;
DROP TABLE IF EXISTS search_runs;
DROP TABLE IF EXISTS search_seeds;
DROP TABLE IF EXISTS providers;

-- ── Liveness and enrolment ────────────────────────────────────────────────

DROP TABLE IF EXISTS enrolments;
DROP TABLE IF EXISTS liveness_sessions;

DROP TYPE IF EXISTS provider_kind;
DROP TYPE IF EXISTS liveness_status;
