-- Reverses 0044 (spec section 3.7). COORDINATED: roll the backend back first -- its engine term
-- reads svc.v_active_scoped_events, optional on its side -- then run this.
--
-- DELIBERATELY DESTRUCTIVE, like 0039's down: every protection credit, live and retracted, goes
-- with the table. Downs run in dev and CI; on a real environment retract every credit and roll
-- the backend back first. The proposals that approved them stay 'applied', with an applied_ref
-- naming an event that no longer exists.

-- A renewal check still queued or running cannot be executed by a step-3 build, so it is ended
-- here. intel_runs.kind has allowed renewal_check since 0039, so no CHECK changes.
UPDATE intel_runs
   SET status = 'failed', error_code = 'migration_down', completed_at = now(),
       lease_expires_at = NULL
 WHERE kind = 'renewal_check' AND status IN ('queued', 'running');

DROP INDEX IF EXISTS intel_runs_one_open_renewal;

-- The threat half alone, byte-identical to 0042's, with its grant re-issued.
CREATE OR REPLACE VIEW svc.v_active_scoped_events AS
SELECT event_id,
       'threat'::text AS direction,
       kind,
       title,
       body,
       severity       AS magnitude,
       tags,
       is_global,
       starts_at,
       expires_at     AS ends_at
  FROM threat_events
 WHERE status = 'active' AND starts_at <= now() AND expires_at > now()
   AND cardinality(tags) > 0;

GRANT SELECT ON svc.v_active_scoped_events TO imageshield_proxy_ro;

-- Its grant goes with it.
DROP TABLE protection_events;
