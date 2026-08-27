-- Reverses 0026. Dropping svc.v_articles breaks the proxy's articles reader
-- (optional on their side -- the feed degrades to empty with a warn log) and
-- nothing here. Coordinated deploy, not a solo rollback.

REVOKE SELECT ON svc.v_articles FROM imageshield_proxy_ro;
DROP VIEW IF EXISTS svc.v_articles;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_services') THEN
    REVOKE content_rw FROM app_services;
  END IF;
END
$$;

DROP TABLE articles;

-- 0022's rule: a cluster-global role survives if another database on the
-- cluster still grants it anything. That is not a failure.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'content_rw') THEN
    REVOKE USAGE ON SCHEMA public FROM content_rw;
    BEGIN
      DROP ROLE content_rw;
    EXCEPTION WHEN dependent_objects_still_exist THEN
      RAISE NOTICE 'role content_rw still holds grants in another database; left in place';
    END;
  END IF;
END
$$;
