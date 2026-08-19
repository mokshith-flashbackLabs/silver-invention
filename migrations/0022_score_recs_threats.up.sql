-- Protection score, recommendations, threat events.
-- Design: docs/superpowers/specs/2026-08-19-protection-score-design.md §4–§6.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'score_rw') THEN
    CREATE ROLE score_rw NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO score_rw;

-- Materialized current score. One row per person; exactly one code path
-- (score/store.py) writes it — INVARIANTS #21 extended, plus a boundary test.
CREATE TABLE protection_scores (
  user_ref       UUID PRIMARY KEY REFERENCES subjects(user_ref),
  score          INT NOT NULL CHECK (score BETWEEN 0 AND 100),
  -- {"posture": int, "coverage": int, "exposure": int, "threat": int}
  components     JSONB NOT NULL,
  config_version TEXT NOT NULL,
  computed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only journal. Every point movement, with a user-readable cause
-- (INVARIANTS #44). BIGSERIAL like audit_log: an ordered journal, not an
-- addressable resource.
CREATE TABLE score_events (
  score_event_id BIGSERIAL PRIMARY KEY,
  user_ref       UUID NOT NULL REFERENCES subjects(user_ref),
  delta          INT NOT NULL,
  component      TEXT NOT NULL
                 CHECK (component IN ('posture', 'coverage', 'exposure', 'threat')),
  cause_kind     TEXT NOT NULL,
  cause_ref      TEXT,
  config_version TEXT NOT NULL,
  score_after    INT NOT NULL CHECK (score_after BETWEEN 0 AND 100),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX score_events_user_idx ON score_events (user_ref, score_event_id DESC);

CREATE TABLE threat_events (
  event_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind        TEXT NOT NULL
              CHECK (kind IN ('leak', 'deepfake_wave', 'platform_incident', 'other')),
  title       TEXT NOT NULL CHECK (title <> ''),
  body        TEXT NOT NULL DEFAULT '',
  severity    SMALLINT NOT NULL CHECK (severity BETWEEN 1 AND 5),
  -- Relevance matcher: hit domains. Flattened from the spec's matcher jsonb so
  -- the join below is SQL, not JSON spelunking.
  domains     TEXT[] NOT NULL DEFAULT '{}',
  is_global   BOOLEAN NOT NULL DEFAULT false,
  penalty     NUMERIC(5,2) NOT NULL CHECK (penalty > 0),
  starts_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at  TIMESTAMPTZ NOT NULL,
  decay_days  INT NOT NULL CHECK (decay_days > 0),
  status      TEXT NOT NULL DEFAULT 'active'
              CHECK (status IN ('draft', 'active', 'expired', 'retracted')),
  created_by  TEXT NOT NULL CHECK (created_by <> ''),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (expires_at > starts_at),
  -- An event that matches nothing is a typo, not a threat.
  CHECK (is_global OR cardinality(domains) > 0)
);

CREATE INDEX threat_events_active_idx ON threat_events (status)
  WHERE status = 'active';

CREATE TABLE threat_event_matches (
  event_id        UUID NOT NULL REFERENCES threat_events(event_id) ON DELETE CASCADE,
  user_ref        UUID NOT NULL REFERENCES subjects(user_ref),
  matched_via     TEXT NOT NULL,   -- the matching domain, or 'global'
  penalty_applied NUMERIC(5,2) NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (event_id, user_ref)
);

CREATE INDEX threat_event_matches_user_idx ON threat_event_matches (user_ref);

CREATE TABLE recommendations (
  rec_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref        UUID NOT NULL REFERENCES subjects(user_ref),
  kind            TEXT NOT NULL CHECK (kind IN
    ('complete_enrolment', 'add_seed_photos', 'refresh_seeds',
     'respond_to_hits', 'run_priority_scan')),
  params          JSONB NOT NULL DEFAULT '{}'::jsonb,
  status          TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'completed', 'expired', 'dismissed')),
  source_event_id UUID REFERENCES threat_events(event_id),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at    TIMESTAMPTZ,
  expires_at      TIMESTAMPTZ
);

-- One open instance per (person, kind, triggering event). The sentinel UUID
-- collapses NULL source_event_id into one bucket for the uniqueness check.
CREATE UNIQUE INDEX recommendations_open_uniq ON recommendations (
  user_ref, kind,
  COALESCE(source_event_id, '00000000-0000-0000-0000-000000000000'::uuid)
) WHERE status = 'open';

CREATE INDEX recommendations_user_open_idx ON recommendations (user_ref)
  WHERE status = 'open';

-- Grants, enumerated (0015's rule). score_events gets NO UPDATE and NO DELETE:
-- an editable journal is not a journal.
GRANT SELECT, INSERT, UPDATE ON
  protection_scores, recommendations, threat_events, threat_event_matches
  TO score_rw;
GRANT SELECT, INSERT ON score_events TO score_rw;
GRANT USAGE ON SEQUENCE score_events_score_event_id_seq TO score_rw;

-- Reach the deployed login role, 0018-style: conditional, idempotent, noisy.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_services') THEN
    GRANT score_rw TO app_services;
    RAISE NOTICE 'granted score_rw to app_services';
  ELSE
    RAISE NOTICE 'role app_services absent; score_rw not granted to it';
  END IF;
END
$$;
