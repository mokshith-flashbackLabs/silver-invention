-- 0047 -- likeness intel: a source's terms note becomes optional, and a source registered from a
-- validation records that evidence instead (owner decision 2026-10-03; spec
-- docs/superpowers/specs/2026-09-27-likeness-intel-design.md section 3.2, amended).
--
-- terms_note was NOT NULL with length >= 10, and in practice was typed as filler on every source
-- of every quiz question. It is now NULL when the operator gives none; a given note is trimmed by
-- the API and is 1 to 500 characters (intel/bounds.py MAX_TERMS_NOTE_CHARS).
--
-- validation_run_id / validated_at: the source_validation run that found the source ready, and
-- when that run completed. Stage 4 (POST /weight-suggestions) sets both on a source it registers;
-- a source created through POST /sources was never validated and carries neither; a reused source
-- is never rewritten. The pair is set together or not at all.

-- 0039 wrote the terms_note CHECK unnamed, so it is found by its definition, never by an assumed
-- name (0043's precedent). No other CHECK on the table mentions the column.
DO $$
DECLARE
  found text;
BEGIN
  FOR found IN
    SELECT conname FROM pg_constraint
     WHERE conrelid = 'intel_sources'::regclass AND contype = 'c'
       AND pg_get_constraintdef(oid) LIKE '%terms_note%'
  LOOP
    EXECUTE format('ALTER TABLE intel_sources DROP CONSTRAINT %I', found);
  END LOOP;
END
$$;
ALTER TABLE intel_sources ALTER COLUMN terms_note DROP NOT NULL;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_terms_note_length
  CHECK (terms_note IS NULL OR length(terms_note) BETWEEN 1 AND 500);

ALTER TABLE intel_sources
  ADD COLUMN validation_run_id UUID REFERENCES intel_runs(run_id),
  ADD COLUMN validated_at TIMESTAMPTZ;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_validation_evidence_paired
  CHECK ((validation_run_id IS NULL) = (validated_at IS NULL));

-- No grant: 0039's table-level grants to intel_rw cover the new columns.
