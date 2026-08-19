DELETE FROM providers WHERE provider_id = 'rekognition_confirm';

DROP TABLE review_tasks;

DROP INDEX infringements_decided_phash_idx;
DROP INDEX infringements_confirm_state_idx;

ALTER TABLE infringements
  DROP CONSTRAINT infringements_duplicate_needs_source,
  DROP CONSTRAINT infringements_confirmed_needs_human;

ALTER TABLE infringements
  DROP COLUMN moderation_labels,
  DROP COLUMN face_match_score,
  DROP COLUMN phash,
  DROP COLUMN duplicate_of,
  DROP COLUMN confirm_decided_at,
  DROP COLUMN confirm_decided_by,
  DROP COLUMN severity,
  DROP COLUMN confirm_state;
