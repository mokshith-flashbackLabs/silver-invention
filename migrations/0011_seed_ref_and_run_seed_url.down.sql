-- Reverses 0011.
--
-- Reverting re-creates the bug this migration exists to fix: seeds go back to
-- being the place a presigned URL is stored, and every seed silently dies about
-- a week after it is created. Reversibility is a CI requirement (build spec
-- Phase 1 §5) so the pair exists; running it in an environment doing real scans
-- restores a failure that is invisible until the second scheduled scan.
--
-- The per-run URLs are dropped outright. They are per-run credentials with
-- minutes of life left, so there is nothing to preserve -- unlike the seed
-- column, which is renamed back rather than dropped so the durable references
-- survive the round trip.
--
-- Recorded here rather than in a runbook because this file is what an operator
-- reads immediately before running it.

ALTER TABLE search_runs DROP COLUMN seed_url;

COMMENT ON COLUMN search_seeds.source_object_ref IS NULL;

ALTER TABLE search_seeds RENAME COLUMN source_object_ref TO source_object_uri;
