-- Grant the outbox to the application. It has never been granted to anything.
--
-- WHAT WAS WRONG. 0001 creates `outbox` and grants it to NO role. 0015 gives
-- the four module roles rights on their own tables and 0018 makes `app_services`
-- a member of all of them, but `outbox` appears in neither list. So the relay,
-- which connects as `app_services`, dies on its first poll with
--
--   psycopg.errors.InsufficientPrivilege: permission denied for table outbox
--
-- and because `relay` is an essential container the whole task stops. ECS
-- restarts it, it dies again, and `search-worker` — which shares that task and
-- is otherwise healthy — is killed with SIGTERM about 200ms after it starts.
-- So a missing GRANT presents as "search is not running", which is the wrong
-- place to look.
--
-- WHY THIS SURVIVED A WHOLE DEV DEPLOY. Dev works. Same migrations, same
-- `app_services` username, relay publishing normally. The privilege was granted
-- there BY HAND during the dev deploy and never written down, so dev proves
-- nothing about whether the migrations are sufficient — it proves only that
-- someone fixed dev once. Production was the first environment where the
-- migrations alone had to stand up, and they did not. That is the actual
-- lesson here, and it is worth more than this grant: an out-of-band fix to a
-- live database makes the next environment fail in a way nobody can predict
-- from the repository.
--
-- WHY A ROLE AND NOT A DIRECT GRANT. Consistency with 0015/0018: privileges
-- attach to a NOLOGIN module role, and login roles become members of it. A
-- direct `GRANT ... TO app_services` would work today and diverge the moment a
-- second login role appears.
--
-- WHY ONE ROLE AND NOT TWO. The producers INSERT (imageshield/outbox.py,
-- search/store.py) and the relay SELECTs and UPDATEs, so a producer/consumer
-- split is expressible. It is not worth it: both run as `app_services` in one
-- process tree, so both memberships would land on the same login role and the
-- split would describe a separation that does not exist. Contrast audit_log,
-- where INSERT-only is a real invariant with a real attacker model and 0001
-- and 0015 both enforce it.
--
-- NO DELETE, deliberately. The relay never deletes: it sets `published_at` and
-- leaves the row. Dead letters (`published_at IS NULL AND attempts >=
-- outbox_max_attempts`) are evidence, and a relay that could delete them could
-- erase the record of what it failed to publish.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'outbox_rw') THEN
    CREATE ROLE outbox_rw NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO outbox_rw;

GRANT SELECT, INSERT, UPDATE ON outbox TO outbox_rw;

-- BIGSERIAL: INSERT needs the sequence as well as the table. Omitting this is
-- the same failure one step later — the INSERT is refused on the sequence
-- rather than the table, with a message naming neither the row nor the queue.
GRANT USAGE ON SEQUENCE outbox_outbox_id_seq TO outbox_rw;

-- Complete the chain, exactly as 0018 does for the other module roles: the
-- grant is conditional on the login role existing, because the cluster
-- bootstrap creates it and a virgin database (CI, docker compose) has no such
-- role. A name that is absent raises a NOTICE and is skipped.
DO $$
DECLARE
  login_role TEXT;
BEGIN
  FOREACH login_role IN ARRAY ARRAY['app_services']
  LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = login_role) THEN
      -- Idempotent: re-granting an existing membership is not an error.
      EXECUTE format('GRANT outbox_rw TO %I', login_role);
      RAISE NOTICE 'granted outbox_rw to %', login_role;
    ELSE
      RAISE NOTICE 'role % absent; outbox_rw not granted to it', login_role;
    END IF;
  END LOOP;
END
$$;
