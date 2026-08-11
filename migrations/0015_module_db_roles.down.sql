-- Reverses 0015.
--
-- Grants must be revoked before a role can be dropped, and a role cannot be
-- dropped while any database on the cluster still grants it anything -- the
-- same constraint 0001's down migration documents for imageshield_app. On a
-- developer machine with more than one database migrated, the DROP is skipped
-- and the role legitimately survives; that is not a failure.

REVOKE ALL ON ALL TABLES IN SCHEMA public
  FROM identity_rw, search_rw, calibration_rw, audit_w;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public
  FROM identity_rw, search_rw, calibration_rw, audit_w;
REVOKE USAGE ON SCHEMA public
  FROM identity_rw, search_rw, calibration_rw, audit_w;

DO $$
DECLARE
  role_name TEXT;
BEGIN
  FOREACH role_name IN ARRAY ARRAY['identity_rw','search_rw','calibration_rw','audit_w']
  LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
      BEGIN
        EXECUTE format('DROP ROLE %I', role_name);
      EXCEPTION WHEN dependent_objects_still_exist THEN
        RAISE NOTICE 'role % still holds grants in another database; left in place', role_name;
      END;
    END IF;
  END LOOP;
END
$$;
