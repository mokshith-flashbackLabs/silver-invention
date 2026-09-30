-- Reverses 0043.
--
-- A source the worker paused as 'unmapped' stays disabled, as if an operator had disabled it:
-- nothing re-enables a source on the way down.
UPDATE intel_sources SET disabled_reason = NULL WHERE disabled_reason = 'unmapped';
ALTER TABLE intel_sources DROP CONSTRAINT intel_sources_disabled_reason_valid;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_disabled_reason_check
  CHECK (disabled_reason IN ('too_short', 'unreachable'));
-- Their CHECKs go with them.
ALTER TABLE intel_sources DROP COLUMN proposed_for, DROP COLUMN origin;

-- A question run still queued or running can no longer be executed, so it is ended here, while
-- the wider CHECK still stands: NOT VALID skips existing rows, but never a later UPDATE of one.
UPDATE intel_runs
   SET status = 'failed', error_code = 'migration_down', completed_at = now(),
       lease_expires_at = NULL
 WHERE kind IN ('source_proposal', 'source_validation') AND status IN ('queued', 'running');
-- Finished runs of the two kinds are history and are kept, so the old CHECK comes back NOT VALID
-- when any exist; it still refuses a NEW run of either kind.
ALTER TABLE intel_runs DROP CONSTRAINT intel_runs_kind_valid;
ALTER TABLE intel_runs ADD CONSTRAINT intel_runs_kind_check
  CHECK (kind IN ('source_check', 'discovery', 'adhoc_url', 'weight_suggestion', 'renewal_check',
                  'gap_regenerate')) NOT VALID;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM intel_runs
                  WHERE kind IN ('source_proposal', 'source_validation')) THEN
    ALTER TABLE intel_runs VALIDATE CONSTRAINT intel_runs_kind_check;
  END IF;
END
$$;
