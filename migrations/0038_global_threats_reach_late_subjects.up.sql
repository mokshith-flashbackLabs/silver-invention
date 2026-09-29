-- 0038 — an "All users" threat event reaches people who enrol AFTER it
-- (owner report, 2026-09-29: "new users are not getting the threat events").
--
-- A global event materialised its matches ONCE, at creation
-- (threats/store.py `_MATCH_GLOBAL_SQL`: `SELECT ... FROM subjects`). So
-- "All users" meant everyone enrolled at that moment, and anybody who
-- became a subject afterwards was matched to nothing. The backend reads
-- svc.v_person_threat_context, which reads only these matches, so a new person
-- carried no threat charge and saw no threat action card for as long as
-- the event lived. On prod that was every person enrolled after
-- 2026-09-25.
--
-- WHY A TRIGGER, not a change to the view. The view is the versioned
-- contract with the backend (CLAUDE.md §3) and stays exactly as it is.
-- `threat_event_matches` also stays the one list of who an event affects:
-- `retract_event` returns it so the backend can recompute those people, and a
-- view-only match would be missing from that list, so retracting an event
-- would leave those people's points unreturned until something else
-- recomputed them.
--
-- SECURITY DEFINER because the INSERT on `subjects` runs as the enrolment
-- module's own role (0015), which has no business writing threat matches and
-- no grant to. A missing grant would make every enrolment fail, and on the
-- enrolment path that is the worst possible failure. The function is owned by
-- the migration role, pins its search_path, and does one thing.
--
-- ONLY GLOBAL EVENTS. A domain event's matches still come from the domain
-- pass at creation: a person matched by relevance is a different question from
-- a person matched by existing, and this migration answers the second only.

CREATE FUNCTION match_new_subject_to_global_threats() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  INSERT INTO threat_event_matches (event_id, user_ref, matched_via)
  SELECT e.event_id, NEW.user_ref, 'global'
    FROM threat_events e
   WHERE e.is_global AND e.status = 'active' AND e.expires_at > now()
  ON CONFLICT DO NOTHING;
  RETURN NULL;
END;
$$;

CREATE TRIGGER subjects_match_global_threats
AFTER INSERT ON subjects
FOR EACH ROW EXECUTE FUNCTION match_new_subject_to_global_threats();

-- The people already missed: every current subject, against every live global
-- event. ON CONFLICT DO NOTHING keeps a domain attribution where one exists,
-- the same rule `_MATCH_GLOBAL_SQL` follows.
INSERT INTO threat_event_matches (event_id, user_ref, matched_via)
SELECT e.event_id, s.user_ref, 'global'
  FROM threat_events e
 CROSS JOIN subjects s
 WHERE e.is_global AND e.status = 'active' AND e.expires_at > now()
ON CONFLICT DO NOTHING;
