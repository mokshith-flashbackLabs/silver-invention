-- Reverse of 0001_initial_schema.up.sql. Revoke the application-role grants
-- and drop the role first (roles are cluster-global; dropping it here does
-- not affect other databases as long as their own grants are already gone),
-- then drop tables in reverse dependency order, then the enum types.

REVOKE USAGE ON SEQUENCE audit_log_audit_id_seq FROM imageshield_app;
REVOKE INSERT ON audit_log FROM imageshield_app;

DROP ROLE IF EXISTS imageshield_app;

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
