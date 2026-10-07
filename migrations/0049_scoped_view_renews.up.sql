-- 0049: svc.v_active_scoped_events names the credit a renewal continues
-- (spec 2026-10-08-scoped-view-renews).
--
-- A protection's strength changes only through a renewal: at the old credit's
-- review date a new event takes over, possibly weaker. The backend wrote that
-- handover as the old credit "no longer counting", and the renewal's own words
-- (the reason, e.g. a platform's policy change) never reached the person. It
-- cannot pair the two without being told: renews_event_id is that pointer.
--
-- APPENDED, never inserted: the view's columns are a versioned contract
-- (CLAUDE.md §3), and adding one at the end is the one change that breaks no
-- reader. NULL for a threat and for a protection that renews nothing.
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
       expires_at     AS ends_at,
       NULL::uuid     AS renews_event_id
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
       review_by,
       renews_event_id
  FROM protection_events
 WHERE status = 'active' AND starts_at <= now() AND review_by > now();

GRANT SELECT ON svc.v_active_scoped_events TO imageshield_proxy_ro;
