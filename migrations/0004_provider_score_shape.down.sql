-- Reversal deletes rows the restored NOT NULL / dropped columns cannot
-- represent (categorical matches, the seeded providers and their calls).
-- Down migrations run in dev/CI only; the data loss is deliberate and
-- documented here.

DELETE FROM search_matches WHERE provider_id IN ('hive', 'google');
DELETE FROM provider_calls WHERE provider_id IN ('hive', 'google');
DELETE FROM search_matches WHERE provider_score IS NULL;

ALTER TABLE search_matches
  DROP CONSTRAINT search_matches_score_shape,
  DROP COLUMN score_kind,
  DROP COLUMN provider_category,
  DROP COLUMN query_quality;
ALTER TABLE search_matches ALTER COLUMN provider_score SET NOT NULL;

ALTER TABLE providers DROP COLUMN score_kind, DROP COLUMN score_domain;

ALTER TABLE search_runs DROP COLUMN status, DROP COLUMN claimed_at;

DELETE FROM providers WHERE provider_id IN ('hive', 'google');
