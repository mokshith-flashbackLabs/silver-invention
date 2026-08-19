DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_services') THEN
    REVOKE score_rw FROM app_services;
  END IF;
END
$$;

DROP TABLE recommendations;
DROP TABLE threat_event_matches;
DROP TABLE threat_events;
DROP TABLE score_events;
DROP TABLE protection_scores;

-- Grants must be revoked before a role can be dropped, and a role cannot be
-- dropped while any database on the cluster still grants it anything -- the
-- same constraint 0001, 0015 and 0016 document (score_rw is cluster-global,
-- exactly like their roles). On a developer machine with more than one
-- database migrated, the DROP is skipped and the role legitimately survives;
-- that is not a failure.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'score_rw') THEN
    REVOKE USAGE ON SCHEMA public FROM score_rw;
    BEGIN
      DROP ROLE score_rw;
    EXCEPTION WHEN dependent_objects_still_exist THEN
      RAISE NOTICE 'role score_rw still holds grants in another database; left in place';
    END;
  END IF;
END
$$;
