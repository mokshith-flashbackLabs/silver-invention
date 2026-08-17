-- Reverses 0018.
--
-- Revokes the memberships and stops there. `app_services` is NOT dropped: it
-- belongs to the cluster bootstrap, other things may hold its password, and
-- dropping a login role to reverse a grant is a much larger action than the one
-- being undone.
--
-- Reverting this takes the `services` deployable offline -- it removes its only
-- path to every table it reads and writes. That makes this down leg a
-- coordinated deploy rather than a rollback anyone runs alone, the same status
-- 0015's and 0017's carry.

DO $$
DECLARE
  login_role TEXT := 'app_services';
  module_role TEXT;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = login_role) THEN
    RETURN;
  END IF;

  FOREACH module_role IN ARRAY
    ARRAY['identity_rw', 'search_rw', 'calibration_rw', 'audit_w', 'imageshield_app']
  LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = module_role) THEN
      EXECUTE format('REVOKE %I FROM %I', module_role, login_role);
    END IF;
  END LOOP;
END
$$;
