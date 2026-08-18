-- Reverses 0020. Revokes the membership only; does not drop the role, which
-- belongs to the cluster bootstrap.
--
-- Reverting this breaks the proxy's MIGRATIONS, not its runtime: their
-- application roles keep their own membership from 0017. So a `CREATE VIEW`
-- against `svc` fails after this, while an already-created view keeps working.

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migrator_backend') THEN
    REVOKE imageshield_proxy_ro FROM migrator_backend;
  END IF;
END
$$;
