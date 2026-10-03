-- Reverses 0047.
--
-- The evidence columns go, with their FK and their paired CHECK. The required note comes back:
-- a source with no note gets a placeholder, and a note shorter than 0039's 10 characters keeps
-- the operator's text with the same words appended, so no row is lost and none fails the
-- restored CHECK.
ALTER TABLE intel_sources DROP COLUMN validated_at, DROP COLUMN validation_run_id;
ALTER TABLE intel_sources DROP CONSTRAINT intel_sources_terms_note_length;
UPDATE intel_sources
   SET terms_note = CASE
         WHEN terms_note IS NULL THEN 'recorded while terms notes were optional'
         ELSE terms_note || ' (recorded while terms notes were optional)' END,
       updated_at = now()
 WHERE terms_note IS NULL OR length(terms_note) < 10;
ALTER TABLE intel_sources ALTER COLUMN terms_note SET NOT NULL;
-- The name Postgres gave 0039's unnamed column CHECK.
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_terms_note_check
  CHECK (length(terms_note) >= 10);
