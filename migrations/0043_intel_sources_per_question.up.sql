-- Likeness intel, step 5 (spec docs/superpowers/specs/2026-09-27-likeness-intel-design.md
-- section 4.10, amended 2026-09-30): sources are chosen per quiz question.
--
-- origin: 'suggested' when a stage-1 source-proposal run proposed the source, 'operator'
-- otherwise, including every source registered through POST /sources. proposed_for is
-- provenance only, {question_key, option}; matching still goes through tags.
ALTER TABLE intel_sources
  ADD COLUMN origin TEXT NOT NULL DEFAULT 'operator',
  ADD COLUMN proposed_for JSONB;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_origin_valid
  CHECK (origin IN ('suggested', 'operator'));
-- coalesce: a missing key yields NULL, and a CHECK that evaluates to NULL passes.
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_proposed_for_shape
  CHECK (proposed_for IS NULL OR (
    jsonb_typeof(proposed_for) = 'object'
    AND coalesce(jsonb_typeof(proposed_for -> 'question_key') = 'string', false)
    AND coalesce(jsonb_typeof(proposed_for -> 'option') = 'string', false)));
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_suggested_names_its_option
  CHECK (origin <> 'suggested' OR proposed_for IS NOT NULL);

-- disabled_reason gains 'unmapped': the worker pauses a source whose non-empty tags are all
-- unmapped in the live vocabulary, and resumes it when one is mapped again (section 4.9). 0039
-- wrote this CHECK unnamed, so it is found by its definition, never by an assumed name.
DO $$
DECLARE
  found text;
BEGIN
  FOR found IN
    SELECT conname FROM pg_constraint
     WHERE conrelid = 'intel_sources'::regclass AND contype = 'c'
       AND pg_get_constraintdef(oid) LIKE '%too_short%'
  LOOP
    EXECUTE format('ALTER TABLE intel_sources DROP CONSTRAINT %I', found);
  END LOOP;
END
$$;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_disabled_reason_valid
  CHECK (disabled_reason IN ('too_short', 'unreachable', 'unmapped'));

-- intel_runs.kind gains the two stages that come before a weight suggestion. Their results live
-- in the run's outcome; no new table. Found by definition: 'adhoc_url' appears in no other CHECK
-- on the table, whether this is 0039's unnamed CHECK or the NOT VALID one this file's down
-- leaves behind.
DO $$
DECLARE
  found text;
BEGIN
  FOR found IN
    SELECT conname FROM pg_constraint
     WHERE conrelid = 'intel_runs'::regclass AND contype = 'c'
       AND pg_get_constraintdef(oid) LIKE '%adhoc_url%'
  LOOP
    EXECUTE format('ALTER TABLE intel_runs DROP CONSTRAINT %I', found);
  END LOOP;
END
$$;
ALTER TABLE intel_runs ADD CONSTRAINT intel_runs_kind_valid
  CHECK (kind IN ('source_check', 'discovery', 'adhoc_url', 'weight_suggestion', 'renewal_check',
                  'gap_regenerate', 'source_proposal', 'source_validation'));

-- No grant: 0039's table-level grants to intel_rw cover the new columns.
