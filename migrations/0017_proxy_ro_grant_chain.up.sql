-- Deploy coordination: complete the grant chain 0016 left half-open.
--
-- WHAT WAS WRONG. 0016 creates `imageshield_proxy_ro` and grants it SELECT on
-- the four `svc` views. That role is NOLOGIN -- deliberately, it is a grant
-- target and not an identity -- and NOTHING HAS EVER BEEN GRANTED MEMBERSHIP
-- IN IT. So the contract the views describe reaches nobody. Until the dev
-- deploy this was invisible: the proxy connected as the database OWNER, which
-- can read everything, so the grant chain landing correctly and the grant
-- chain being bypassed looked identical. In dev the proxy connects as
-- `app_backend`, which inherits nothing from ownership, so the same gap now
-- presents as their report screen failing on its first read and their /readyz
-- never going green -- and it presents as "the svc views are missing", which
-- is the wrong place to look.
--
-- WHY THE GRANT IS OURS TO RUN. Membership in a role requires ADMIN OPTION on
-- that role. `imageshield_proxy_ro` was created by this repo's migration
-- runner (0016), so this repo's runner holds it implicitly and the proxy's
-- migrator does not. They cannot complete this chain from their side however
-- much they want to. If this migration fails with `must have admin option on
-- role "imageshield_proxy_ro"`, 0016 was applied by a different role than this
-- one -- fix that rather than granting the runner more privilege.
--
-- WHY THREE NAMES AND NOT ONE. The tidy form is a single
-- `GRANT imageshield_proxy_ro TO imageshield_proxy`: their umbrella role, which
-- their 0012/0013 make `imageshield_app`, `app_backend` and `app_worker` all
-- members of, so one grant reaches every login role they have now or add
-- later. But `imageshield_proxy` only exists after THEIR 0001 has applied, and
-- the deploy order is services-first -- we create `svc` before their migrator
-- runs, because their /readyz cannot pass without the four views
-- (DEPLOY-DEV.md §7.6, D13). On a virgin database the tidy form would grant
-- nothing at all and fail silently.
--
-- So the grant is conditional on each name existing, and all three are
-- enumerated. On the first deploy `app_backend` and `app_worker` exist (the
-- cluster bootstrap creates them, before either repo migrates) and
-- `imageshield_proxy` does not; on every deploy after that all three do and the
-- redundant grants are harmless. Whichever order the two repos run in, the
-- chain completes.
--
-- WHAT THIS DELIBERATELY DOES NOT DO. It does not CREATE any of these roles.
-- They belong to the other repo and to the cluster bootstrap; a migration here
-- that conjured `app_backend` would make a missing deploy step look like a
-- successful one, and D12's rule -- neither repo's migrations create anything
-- in the other's -- is worth more than the convenience. A name that is absent
-- raises a NOTICE and is skipped, which is also why this migration is a
-- successful no-op in CI and in docker compose, where no proxy role exists.

DO $$
DECLARE
  target TEXT;
BEGIN
  FOREACH target IN ARRAY ARRAY['imageshield_proxy', 'app_backend', 'app_worker']
  LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = target) THEN
      -- Idempotent: re-granting an existing membership is not an error.
      EXECUTE format('GRANT imageshield_proxy_ro TO %I', target);
      RAISE NOTICE 'granted imageshield_proxy_ro to %', target;
    ELSE
      RAISE NOTICE 'role % absent; imageshield_proxy_ro not granted to it', target;
    END IF;
  END LOOP;
END
$$;
