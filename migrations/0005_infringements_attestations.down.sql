-- Reverses 0005's shape.
--
-- Rows migrated INTO infringements/attestations are not copied back into
-- search_matches. That is lossy on purpose and safe in this direction: the
-- up-migration's source rows are still in search_matches (0006 is what drops
-- them), so reverting 0005 loses only observations recorded AFTER it ran.

DROP TABLE attestations;
DROP TABLE infringements;

ALTER TABLE content_urls
  DROP COLUMN normalisation_version,
  DROP COLUMN canonical_url;

-- Restoring NOT NULL requires filling anything the retention job nulled.
-- An empty object is the honest marker: the payload is gone, not empty.
UPDATE provider_calls SET raw_response = '{}'::jsonb WHERE raw_response IS NULL;
ALTER TABLE provider_calls ALTER COLUMN raw_response SET NOT NULL;
