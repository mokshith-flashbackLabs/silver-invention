-- Articles: operator-authored content for the app's feed, plus the ninth
-- svc contract view that carries published rows to the proxy.
-- Design: docs/superpowers/specs/2026-08-27-articles-design.md §4.
--
-- ADDITIVE ONLY. New table, new view, new role; no existing relation changes.
-- Articles are not identity data: no user_ref anywhere here, so the view
-- needs no per-person scoping (spec §3).

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'content_rw') THEN
    CREATE ROLE content_rw NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO content_rw;

CREATE TABLE articles (
  article_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title        TEXT NOT NULL CHECK (title <> ''),
  summary      TEXT NOT NULL DEFAULT '',
  body         TEXT NOT NULL DEFAULT '',
  -- [{"url": "https://...", "alt": "..."}], hero first. Pasted URLs, never
  -- bytes (INVARIANTS #9): this repo holds no S3 credentials, and this table
  -- holds no picture, only where one is.
  images       JSONB NOT NULL DEFAULT '[]'::jsonb,
  -- [{"name": "...", "url": "https://..."}]
  sources      JSONB NOT NULL DEFAULT '[]'::jsonb,
  status       TEXT NOT NULL DEFAULT 'draft'
               CHECK (status IN ('draft', 'published', 'archived')),
  published_at TIMESTAMPTZ,
  created_by   TEXT NOT NULL CHECK (created_by <> ''),
  updated_by   TEXT NOT NULL CHECK (updated_by <> ''),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (jsonb_typeof(images) = 'array'),
  CHECK (jsonb_typeof(sources) = 'array'),
  -- A draft has never been published; a published row always says when.
  -- Archived carries whichever it had: a draft archived unpublished has
  -- none, a published article archived keeps its date.
  CHECK (status <> 'draft' OR published_at IS NULL),
  CHECK (status <> 'published' OR published_at IS NOT NULL)
);

-- The feed's order and its keyset cursor: newest first, id as the tiebreak.
CREATE INDEX articles_published_idx ON articles (published_at DESC, article_id DESC)
  WHERE status = 'published';

-- Enumerated grants (0015's rule). No DELETE: archive is the soft delete.
GRANT SELECT, INSERT, UPDATE ON articles TO content_rw;

-- Reach the deployed login role, 0018/0022-style: conditional, idempotent, noisy.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_services') THEN
    GRANT content_rw TO app_services;
    RAISE NOTICE 'granted content_rw to app_services';
  ELSE
    RAISE NOTICE 'role app_services absent; content_rw not granted to it';
  END IF;
END
$$;

-- The ninth contract view. Published rows only: a draft or an archived
-- article is not the proxy's to see. Same versioned-contract rule as the
-- other eight (0016): add columns freely, never remove or retype one without
-- a coordinated deploy.
CREATE VIEW svc.v_articles AS
SELECT article_id, title, summary, body, images, sources, published_at, updated_at
FROM articles
WHERE status = 'published';

GRANT SELECT ON svc.v_articles TO imageshield_proxy_ro;
