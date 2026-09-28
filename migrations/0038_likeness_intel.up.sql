-- Likeness intel, step 1 (spec docs/superpowers/specs/2026-09-27-likeness-intel-design.md §3).
--
-- providers.kind BECOMES TEXT. scripts/migrate.py runs each pending migration in
-- its own transaction, so `ALTER TYPE provider_kind ADD VALUE 'llm'` could never be
-- used by the INSERT below in the same file — a just-added enum label is not visible
-- to the transaction that added it. Only tests/test_migrations.py named the type.
--
-- No threat or protection change and no svc view here: those are 0039/0040 (steps 3/4).

ALTER TABLE providers ALTER COLUMN kind TYPE text USING kind::text;
ALTER TABLE providers ADD CONSTRAINT providers_kind_valid
  CHECK (kind IN ('image_search', 'face_search', 'classifier', 'llm'));
DROP TYPE provider_kind;

-- calibrate trust calls set_calibrated with no kind check; the guarantee lives at the write.
ALTER TABLE providers ADD CONSTRAINT providers_llm_never_calibrated
  CHECK (kind <> 'llm' OR NOT calibrated);

-- Shape of every tags text[] column (spec §3.1). Membership is checked in code
-- against the backend-pushed vocabulary: the registry lives in the other repo.
CREATE FUNCTION intel_tags_well_formed(tags text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT array_position(tags, NULL) IS NULL
     AND cardinality(tags) = (SELECT count(DISTINCT u) FROM unnest(tags) AS u)
     AND coalesce((SELECT bool_and(t ~ '^[a-z][a-z0-9_]{0,39}$') FROM unnest(tags) AS t), true)
$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'intel_rw') THEN
    CREATE ROLE intel_rw NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO intel_rw;

CREATE TABLE intel_sources (
  source_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind                  TEXT NOT NULL CHECK (kind IN ('policy_page', 'feed', 'news', 'breach_index',
                                                      'regulator', 'research', 'search_query')),
  source_url            TEXT,
  url_hash              TEXT,
  normalisation_version TEXT,
  query_text            TEXT,
  tags                  TEXT[] NOT NULL DEFAULT '{}' CHECK (intel_tags_well_formed(tags)),
  check_every_hours     INT NOT NULL CHECK (check_every_hours BETWEEN 6 AND 720),
  next_check_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  enabled               BOOLEAN NOT NULL DEFAULT true,
  terms_note            TEXT NOT NULL CHECK (length(terms_note) >= 10),
  last_content_sha256   TEXT,
  last_checked_at       TIMESTAMPTZ,
  last_run_status       TEXT,
  consecutive_failures  SMALLINT NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
  disabled_reason       TEXT CHECK (disabled_reason IN ('too_short', 'unreachable')),
  created_by            TEXT NOT NULL CHECK (created_by <> ''),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ((kind = 'search_query') = (source_url IS NULL)),
  CHECK ((kind = 'search_query') = (query_text IS NOT NULL)),
  CHECK ((source_url IS NULL) = (url_hash IS NULL)),
  CHECK ((source_url IS NULL) = (normalisation_version IS NULL)),
  CHECK (disabled_reason IS NULL OR NOT enabled)
);
CREATE UNIQUE INDEX intel_sources_url_hash_uniq ON intel_sources (url_hash) WHERE url_hash IS NOT NULL;
CREATE INDEX intel_sources_due_idx ON intel_sources (next_check_at) WHERE enabled;

-- Latest normalised text of a policy_page only, PII-masked (INVARIANTS #9 amended).
CREATE TABLE intel_snapshots (
  source_id      UUID PRIMARY KEY REFERENCES intel_sources(source_id),
  content_sha256 TEXT NOT NULL,
  snapshot_text  TEXT NOT NULL,
  content_type   TEXT NOT NULL,
  truncated      BOOLEAN NOT NULL,
  fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE intel_runs (
  run_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind                   TEXT NOT NULL CHECK (kind IN ('source_check', 'discovery', 'adhoc_url',
                                                       'weight_suggestion', 'renewal_check',
                                                       'gap_regenerate')),
  source_id              UUID REFERENCES intel_sources(source_id),
  request                JSONB NOT NULL DEFAULT '{}'::jsonb,
  status                 TEXT NOT NULL DEFAULT 'queued'
                         CHECK (status IN ('queued', 'running', 'completed', 'failed', 'refused')),
  attempts               SMALLINT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_expires_at       TIMESTAMPTZ,
  proposals_written_at   TIMESTAMPTZ,
  vocabulary_release_no  BIGINT,
  vocabulary_map_version BIGINT,
  requested_by           TEXT NOT NULL CHECK (requested_by <> ''),
  outcome                JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_code             TEXT,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at             TIMESTAMPTZ,
  completed_at           TIMESTAMPTZ,
  CHECK ((kind IN ('source_check', 'discovery')) = (source_id IS NOT NULL))
);
CREATE UNIQUE INDEX intel_runs_one_open_per_source ON intel_runs (source_id)
  WHERE status IN ('queued', 'running');
CREATE INDEX intel_runs_claim_idx ON intel_runs (created_at) WHERE status IN ('queued', 'running');

CREATE TABLE intel_documents (
  document_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id                UUID NOT NULL REFERENCES intel_runs(run_id),
  source_id             UUID REFERENCES intel_sources(source_id),
  document_url          TEXT NOT NULL,
  final_url             TEXT NOT NULL,
  url_hash              TEXT NOT NULL,
  -- The normalised hash of the REQUESTED url (equal to url_hash when the
  -- fetch had no redirect). Task 7 writes it; task 10's seen/recent checks
  -- match EITHER hash, because a redirecting url would otherwise be
  -- re-fetched (and re-billed) on every run that only ever sees final_url.
  document_url_hash     TEXT NOT NULL,
  normalisation_version TEXT NOT NULL,
  publisher_domain      TEXT NOT NULL,
  trust                 TEXT NOT NULL CHECK (trust IN ('listed', 'web')),
  content_sha256        TEXT NOT NULL,
  truncated             BOOLEAN NOT NULL,
  title                 TEXT NOT NULL DEFAULT '',
  published_at          TIMESTAMPTZ,
  fetched_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (run_id, url_hash)
);
CREATE INDEX intel_documents_source_url_idx ON intel_documents (source_id, url_hash);
CREATE INDEX intel_documents_recent_idx ON intel_documents (url_hash, fetched_at);
CREATE INDEX intel_documents_recent_by_requested_idx ON intel_documents (document_url_hash, fetched_at);

CREATE TABLE intel_signals (
  signal_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id            UUID NOT NULL REFERENCES intel_documents(document_id),
  category               TEXT NOT NULL CHECK (category IN ('policy', 'incident', 'tooling',
                                                           'protection', 'law', 'research')),
  direction              TEXT NOT NULL CHECK (direction IN ('risk_up', 'risk_down', 'neutral')),
  tags                   TEXT[] NOT NULL DEFAULT '{}' CHECK (intel_tags_well_formed(tags)),
  unregistered_subjects  TEXT[] NOT NULL DEFAULT '{}',
  summary                TEXT NOT NULL CHECK (length(summary) <= 500),
  model_id               TEXT NOT NULL,
  prompt_version         TEXT NOT NULL,
  status                 TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retracted')),
  retracted_by           TEXT,
  retracted_at           TIMESTAMPTZ,
  retract_reason         TEXT,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ((status = 'retracted') = (retracted_by IS NOT NULL AND retracted_at IS NOT NULL
                                   AND retract_reason IS NOT NULL))
);
CREATE INDEX intel_signals_recent_idx ON intel_signals (created_at) WHERE status = 'active';

CREATE TABLE intel_excerpts (
  excerpt_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_id    UUID NOT NULL REFERENCES intel_signals(signal_id),
  quote_text   TEXT NOT NULL CHECK (length(quote_text) BETWEEN 20 AND 600),
  char_start   INT NOT NULL CHECK (char_start >= 0),
  char_end     INT NOT NULL,
  quote_sha256 TEXT NOT NULL,
  CHECK (char_end > char_start)
);

CREATE TABLE intel_proposals (
  proposal_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind                    TEXT NOT NULL CHECK (kind IN ('weight_change', 'threat_event', 'protection_event',
                                                        'weight_suggestion', 'coverage_gap')),
  status                  TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'superseded',
                                                          'applied', 'delivered')),
  supersede_reason        TEXT CHECK (supersede_reason IN ('newer_proposal', 'cell_changed',
                                                           'resolved_by_quiz')),
  target                  JSONB NOT NULL,
  suggested               JSONB NOT NULL,
  decided                 JSONB,
  rationale               TEXT NOT NULL,
  against_scoring_version TEXT,
  against_release_no      BIGINT,
  run_id                  UUID REFERENCES intel_runs(run_id),
  model_id                TEXT NOT NULL,
  prompt_version          TEXT NOT NULL,
  decided_by              TEXT,
  decided_at              TIMESTAMPTZ,
  decision_reason         TEXT,
  applied_ref             TEXT,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- A proposal cannot reach approved without a name on it (INVARIANTS #48).
  CHECK (status NOT IN ('approved', 'rejected', 'applied')
         OR (decided_by IS NOT NULL AND decided_at IS NOT NULL AND decision_reason IS NOT NULL)),
  CHECK (status <> 'pending' OR (decided_by IS NULL AND decided_at IS NULL AND decision_reason IS NULL)),
  -- ...nor without its exact numbers stored.
  CHECK (NOT (status IN ('approved', 'applied')
              AND kind IN ('weight_change', 'threat_event', 'protection_event')) OR decided IS NOT NULL),
  CHECK (status <> 'delivered' OR kind = 'weight_suggestion'),
  CHECK (kind <> 'weight_suggestion' OR status IN ('delivered', 'superseded')),
  CHECK ((status = 'superseded') = (supersede_reason IS NOT NULL))
);
-- At most one approved, unapplied weight change per cell (spec §3.6).
CREATE UNIQUE INDEX intel_proposals_one_approved_per_cell
  ON intel_proposals ((target->>'question_key'), (target->>'option'))
  WHERE kind = 'weight_change' AND status = 'approved';
CREATE INDEX intel_proposals_queue_idx ON intel_proposals (status, kind, created_at);

CREATE TABLE intel_proposal_signals (
  proposal_id UUID NOT NULL REFERENCES intel_proposals(proposal_id),
  signal_id   UUID NOT NULL REFERENCES intel_signals(signal_id),
  PRIMARY KEY (proposal_id, signal_id)
);

CREATE TABLE intel_vocabulary (
  id                     SMALLINT PRIMARY KEY CHECK (id = 1),
  release_no             BIGINT NOT NULL,
  map_version            BIGINT NOT NULL,
  scoring_version        TEXT NOT NULL,
  quiz_version           TEXT NOT NULL,
  document               JSONB NOT NULL,
  received_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  reconciled_release_no  BIGINT,
  reconciled_map_version BIGINT
);

ALTER TABLE provider_calls ADD COLUMN intel_run_id UUID REFERENCES intel_runs(run_id);
ALTER TABLE provider_calls ADD CONSTRAINT provider_calls_one_run
  CHECK (num_nonnulls(run_id, intel_run_id) <= 1);

-- The model's metering row. Disabled until a budget is set and an operator enables
-- it (spec §5). cost_per_call_usd is the step-0 WORST-CASE estimate (Sonnet 5,
-- the model this build actually uses; Opus 5.5 would be 0.45). daily_budget_usd is
-- NULL: the owner has not chosen a figure, and per invariant #38 a NULL cost or
-- budget refuses to dispatch rather than running uncapped — spec §3.9 is where the
-- budget becomes its own migration once a number exists.
INSERT INTO providers (provider_id, kind, enabled, calibrated, score_version,
                       cost_per_call_usd, daily_budget_usd, score_kind, score_domain)
VALUES ('claude_intel', 'llm', false, false, 'n/a',
        0.250000, NULL, 'numeric', NULL)
ON CONFLICT (provider_id) DO NOTHING;

-- Enumerated grants (0015's rule). No DELETE anywhere.
GRANT SELECT, INSERT, UPDATE ON intel_sources, intel_snapshots, intel_runs, intel_documents,
  intel_signals, intel_excerpts, intel_proposals, intel_proposal_signals, intel_vocabulary TO intel_rw;
GRANT SELECT, UPDATE ON providers TO intel_rw;
GRANT SELECT, INSERT, UPDATE ON provider_calls, provider_spend TO intel_rw;
GRANT INSERT ON audit_log TO intel_rw;
GRANT USAGE ON SEQUENCE audit_log_audit_id_seq TO intel_rw;
-- The ONE read of an infringement-adjacent table: whether a hash is a known hit
-- location, so intel never fetches one (spec §6.1). Nothing else of content_urls.
GRANT SELECT (url_hash) ON content_urls TO intel_rw;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_services') THEN
    GRANT intel_rw TO app_services;
    RAISE NOTICE 'granted intel_rw to app_services';
  ELSE
    RAISE NOTICE 'role app_services absent; intel_rw not granted to it';
  END IF;
END
$$;
