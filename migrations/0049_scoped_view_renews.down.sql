-- Reverses 0049. CREATE OR REPLACE cannot drop a column, so the view is
-- dropped and re-created exactly as 0044 left it, and its grant restored.
DROP VIEW svc.v_active_scoped_events;
CREATE VIEW svc.v_active_scoped_events AS
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
   AND cardinality(tags) > 0
UNION ALL
SELECT event_id,
       'protection'::text,
       'protection'::text,
       title,
       body,
       strength,
       tags,
       is_global,
       starts_at,
       review_by
  FROM protection_events
 WHERE status = 'active' AND starts_at <= now() AND review_by > now();

GRANT SELECT ON svc.v_active_scoped_events TO imageshield_proxy_ro;
