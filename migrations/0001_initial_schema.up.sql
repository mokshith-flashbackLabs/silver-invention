-- ── Liveness and enrolment ────────────────────────────────────────────────

CREATE TYPE liveness_status AS ENUM (
  'created','pending','passed','failed','expired','consumed'
);

CREATE TABLE liveness_sessions (
  session_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref             UUID NOT NULL,
  provider             TEXT NOT NULL DEFAULT 'rekognition',
  provider_session_id  TEXT UNIQUE NOT NULL,
  status               liveness_status NOT NULL DEFAULT 'created',
  confidence           NUMERIC(5,2),
  failure_reason       TEXT,
  attempt_number       INT NOT NULL DEFAULT 1,
  reference_image_uri  TEXT,
  audit_image_uris     TEXT[],
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at         TIMESTAMPTZ,
  expires_at           TIMESTAMPTZ NOT NULL,
  consumed_at          TIMESTAMPTZ
);

CREATE INDEX liveness_user_idx ON liveness_sessions (user_ref, created_at DESC);
CREATE INDEX liveness_attempts_idx ON liveness_sessions (user_ref, created_at)
  WHERE status IN ('failed','expired');

CREATE TABLE enrolments (
  enrolment_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id        UUID NOT NULL UNIQUE REFERENCES liveness_sessions(session_id),
  user_ref          UUID NOT NULL,
  collection_id     TEXT NOT NULL,
  external_face_id  TEXT NOT NULL,
  quality_score     NUMERIC(5,2),
  model_id          TEXT NOT NULL,
  source_object_uri TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'active',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at        TIMESTAMPTZ
);

CREATE UNIQUE INDEX enrolments_face_uniq
  ON enrolments (collection_id, external_face_id);
CREATE INDEX enrolments_user_active_idx
  ON enrolments (user_ref) WHERE status = 'active';

-- ── Providers and classification ──────────────────────────────────────────

CREATE TYPE provider_kind AS ENUM (
  'image_search',   -- finds THIS IMAGE and near-variants (Hive media search, TinEye)
  'face_search',    -- finds a PERSON in novel images (PimEyes-class) — none integrated yet
  'classifier'      -- judges a given image (Hive deepfake detection) — later
);

CREATE TABLE providers (
  provider_id     TEXT PRIMARY KEY,
  kind            provider_kind NOT NULL,
  enabled         BOOLEAN NOT NULL DEFAULT false,
  calibrated      BOOLEAN NOT NULL DEFAULT false,
  score_version   TEXT NOT NULL,
  daily_budget_usd NUMERIC(10,2),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- What we search WITH. The seed determines what can be found.
CREATE TABLE search_seeds (
  seed_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref          UUID NOT NULL,
  seed_kind         TEXT NOT NULL,   -- enrolment|user_supplied|public_profile
  source_object_uri TEXT NOT NULL,   -- proxy's S3; presigned at search time
  status            TEXT NOT NULL DEFAULT 'active',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX seeds_user_idx ON search_seeds (user_ref) WHERE status = 'active';

CREATE TABLE search_runs (
  run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  seed_id         UUID NOT NULL REFERENCES search_seeds(seed_id),
  user_ref        UUID NOT NULL,
  providers_attempted TEXT[] NOT NULL,
  providers_succeeded TEXT[] NOT NULL DEFAULT '{}',
  threshold_config JSONB NOT NULL,
  matches_found   INT NOT NULL DEFAULT 0,
  started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at    TIMESTAMPTZ
);

CREATE TABLE content_urls (
  url_hash        TEXT PRIMARY KEY,   -- sha256 of the NORMALISED url
  url             TEXT NOT NULL,
  source_domain   TEXT NOT NULL,
  first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX content_domain_idx ON content_urls (source_domain);

CREATE TABLE provider_calls (
  call_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id        UUID REFERENCES search_runs(run_id),
  provider_id   TEXT NOT NULL REFERENCES providers(provider_id),
  status        TEXT NOT NULL,   -- ok|error|rate_limited|timeout|budget_exceeded
  http_status   INT,
  latency_ms    INT,
  cost_usd      NUMERIC(10,4),
  attempt       INT NOT NULL DEFAULT 1,
  error_detail  TEXT,
  raw_response  JSONB NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX provider_calls_cost_idx ON provider_calls (provider_id, created_at);

-- One row per (page found, provider that found it). Same page from two
-- providers is two attestations of one infringement, not two infringements.
CREATE TABLE search_matches (
  match_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id          UUID NOT NULL REFERENCES search_runs(run_id),
  url_hash        TEXT NOT NULL REFERENCES content_urls(url_hash),
  user_ref        UUID NOT NULL,
  provider_id     TEXT NOT NULL REFERENCES providers(provider_id),
  image_url       TEXT NOT NULL,   -- the matched image itself
  page_url        TEXT,            -- host page, from backlinks[0] when present
  provider_score  NUMERIC(6,4) NOT NULL,  -- RAW, provider-native. NEVER rescaled
  score_version   TEXT NOT NULL,
  band            TEXT NOT NULL,   -- auto_confirm|review|drop
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX search_matches_uniq
  ON search_matches (run_id, url_hash, provider_id);
CREATE INDEX search_matches_user_idx ON search_matches (user_ref, created_at DESC);
CREATE INDEX search_matches_review_idx ON search_matches (band, created_at)
  WHERE band = 'review';

-- ── Outbox ────────────────────────────────────────────────────────────────

CREATE TABLE outbox (
  outbox_id     BIGSERIAL PRIMARY KEY,
  queue_name    TEXT NOT NULL,
  payload       JSONB NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at  TIMESTAMPTZ,
  attempts      INT NOT NULL DEFAULT 0,
  last_error    TEXT
);

CREATE INDEX outbox_unpublished_idx ON outbox (created_at)
  WHERE published_at IS NULL;

-- ── Audit ─────────────────────────────────────────────────────────────────

CREATE TABLE audit_log (
  audit_id      BIGSERIAL PRIMARY KEY,
  actor_type    TEXT NOT NULL,
  action        TEXT NOT NULL,
  subject_ref   UUID,
  resource_id   UUID,
  metadata      JSONB,
  occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Application role ──────────────────────────────────────────────────────
-- Roles are cluster-global, not per-database: a leftover role from a
-- previously dropped database must not make this migration fail on a fresh
-- one, hence the existence guard rather than a bare CREATE ROLE.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'imageshield_app') THEN
    CREATE ROLE imageshield_app NOLOGIN;
  END IF;
END
$$;

-- INSERT-only: audit_log is append-only. No UPDATE, no DELETE, no TRUNCATE,
-- nothing else granted in this migration.
GRANT INSERT ON audit_log TO imageshield_app;
GRANT USAGE ON SEQUENCE audit_log_audit_id_seq TO imageshield_app;
