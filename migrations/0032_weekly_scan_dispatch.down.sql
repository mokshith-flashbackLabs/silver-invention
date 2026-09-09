-- Reverses 0032. `tier_next_scan_after` is derived -- search/cadence.py
-- recomputes it on every completed run -- so dropping it destroys only the
-- accumulated counterfactual, not any state the system needs to run.
--
-- The comment restored below is the 0031-era one: before this migration,
-- next_scan_after carried the tier's interval rather than the coming Sunday.

ALTER TABLE search_seeds
  DROP COLUMN IF EXISTS tier_next_scan_after;

COMMENT ON COLUMN search_seeds.next_scan_after IS
  'When the adaptive cadence (search/cadence.py) says this seed is next due. '
  'Written on every completed run and exposed on the run-status response '
  '(INVARIANTS #42).';
