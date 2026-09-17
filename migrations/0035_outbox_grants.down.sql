-- Reverse 0035.
--
-- Two ordering rules, both learned the hard way elsewhere in this folder:
--
-- 1. Revoke before dropping. A role holding privileges cannot be dropped, and
--    the error ("role outbox_rw cannot be dropped because some objects depend
--    on it") names the role but not the grant.
--
-- 2. ROLES ARE CLUSTER-GLOBAL, TABLES ARE NOT. Two databases on one server can
--    both have this migration applied — two concurrent pytest sessions against
--    one compose Postgres, or a second app database sharing the cluster. The
--    revokes above only reach THIS database's objects, so the role may still be
--    granted in the other one, and an unguarded DROP ROLE then fails and takes
--    this database's teardown down with it. Same shape as 0001's handling of
--    `imageshield_app`, and `test_down_all_on_one_db_does_not_break_role_for_sibling_db`
--    is the test that fails when this is written the obvious way.

DO $$
DECLARE
  login_role TEXT;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'outbox_rw') THEN
    RETURN;
  END IF;

  FOREACH login_role IN ARRAY ARRAY['app_services']
  LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = login_role) THEN
      EXECUTE format('REVOKE outbox_rw FROM %I', login_role);
    END IF;
  END LOOP;

  REVOKE USAGE ON SEQUENCE outbox_outbox_id_seq FROM outbox_rw;
  REVOKE SELECT, INSERT, UPDATE ON outbox FROM outbox_rw;
  REVOKE USAGE ON SCHEMA public FROM outbox_rw;
END
$$;

DO $$
BEGIN
  DROP ROLE IF EXISTS outbox_rw;
EXCEPTION
  WHEN dependent_objects_still_exist THEN
    RAISE NOTICE 'outbox_rw still referenced by another database on this server; leaving role in place';
END
$$;
