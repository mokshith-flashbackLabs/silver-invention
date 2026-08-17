-- Reverses 0017.
--
-- Revokes the membership and stops there. The three roles are NOT dropped:
-- `imageshield_proxy` belongs to the proxy's migrations, `app_backend` and
-- `app_worker` to the cluster bootstrap, and dropping another owner's login
-- role to reverse a grant we made is a much larger action than the one being
-- undone -- it would take their `api` and `worker` offline to roll back a
-- SELECT.
--
-- Reverting this is a COORDINATED DEPLOY, not a rollback anyone runs alone --
-- the same status 0016's down leg carries, and for the same reason. It removes
-- the proxy's only path to the four views, so their report screen and their
-- /readyz fail immediately afterwards.

DO $$
DECLARE
  target TEXT;
BEGIN
  FOREACH target IN ARRAY ARRAY['imageshield_proxy', 'app_backend', 'app_worker']
  LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = target) THEN
      EXECUTE format('REVOKE imageshield_proxy_ro FROM %I', target);
    END IF;
  END LOOP;
END
$$;
