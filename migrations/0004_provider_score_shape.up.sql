-- Step 5: one search_matches table must hold BOTH score shapes without the
-- adapter inventing a number for categorical providers (Google Web Detection
-- returns score: null — normalising in the adapter would make recalibration
-- impossible without a redeploy, CLAUDE.md §7.2).

ALTER TABLE search_matches
  ALTER COLUMN provider_score DROP NOT NULL,
  ADD COLUMN score_kind TEXT NOT NULL DEFAULT 'numeric',
  ADD COLUMN provider_category TEXT,
  ADD COLUMN query_quality TEXT,
  ADD CONSTRAINT search_matches_score_shape CHECK (
    (score_kind = 'numeric'     AND provider_score    IS NOT NULL) OR
    (score_kind = 'categorical' AND provider_category IS NOT NULL)
  );
ALTER TABLE search_matches ALTER COLUMN score_kind DROP DEFAULT;

-- Record each provider's score domain so step 7 calibrates against reality:
-- Hive Web Search reports 0.5–1.0 where 0.5 is the FLOOR (the lowest score
-- Hive will report), not a midpoint meaning "uncertain". Banding it as
-- though it were 0–1 would read weak matches as moderate ones.
ALTER TABLE providers
  ADD COLUMN score_kind   TEXT NOT NULL DEFAULT 'numeric',
  ADD COLUMN score_domain JSONB;

-- Async execution state for the search:runs worker. 'queued' -> 'running'
-- (claimed_at set) -> 'completed'. A 'running' row whose claimed_at is stale
-- is reclaimable (worker died mid-run; SQS redelivers the message).
ALTER TABLE search_runs
  ADD COLUMN status     TEXT NOT NULL DEFAULT 'queued',
  ADD COLUMN claimed_at TIMESTAMPTZ;

-- The two step-5 providers. Which Hive PRODUCT a key hits is determined by
-- the Hive project the key belongs to, not the URL — a key provisioned
-- against Hive's "Media Search" (movies/TV matching) returns plausible-
-- looking wrong results, not an error. Ours must be Web Search (reverse
-- image search, ~25B indexed images). See devtools/harness/README.md.
INSERT INTO providers (provider_id, kind, enabled, calibrated, score_version,
                       score_kind, score_domain)
VALUES
  ('hive',   'image_search', true, false, 'hive-web-search-v1',
   'numeric',     '{"min": 0.5, "max": 1.0}'),
  ('google', 'image_search', true, false, 'google-web-detection-v1',
   'categorical', '{"categories": ["full_match", "partial_match", "page_match"]}')
ON CONFLICT (provider_id) DO NOTHING;
