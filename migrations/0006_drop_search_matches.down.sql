-- Recreates the table as 0001 + 0004 left it, EMPTY. The rows are not
-- restored: their content lives on in infringements/attestations, and
-- reconstructing per-run rows from a deduplicated model would be inventing
-- history rather than reversing a change.
--
-- Reverting past this point is a schema rollback, not a data rollback.

CREATE TABLE search_matches (
  match_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id            UUID NOT NULL REFERENCES search_runs(run_id),
  url_hash          TEXT NOT NULL REFERENCES content_urls(url_hash),
  user_ref          UUID NOT NULL,
  provider_id       TEXT NOT NULL REFERENCES providers(provider_id),
  image_url         TEXT NOT NULL,
  page_url          TEXT,
  provider_score    NUMERIC(6,4),
  score_version     TEXT NOT NULL,
  band              TEXT NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  score_kind        TEXT NOT NULL,
  provider_category TEXT,
  query_quality     TEXT,
  CONSTRAINT search_matches_score_shape CHECK (
    (score_kind = 'numeric'     AND provider_score    IS NOT NULL) OR
    (score_kind = 'categorical' AND provider_category IS NOT NULL)
  )
);

CREATE UNIQUE INDEX search_matches_uniq
  ON search_matches (run_id, url_hash, provider_id);
CREATE INDEX search_matches_user_idx ON search_matches (user_ref, created_at DESC);
CREATE INDEX search_matches_review_idx ON search_matches (band, created_at)
  WHERE band = 'review';
