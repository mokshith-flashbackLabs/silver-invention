-- Back to 0011's comment, which is the last one this column carried.
--
-- Reverting the COMMENT does NOT retire any 'face_crop' row already written,
-- and must not pretend to: rewriting those rows to 'user_supplied' would erase
-- the distinction rather than revert it, and rewriting them to the photo_ref
-- is impossible -- the seed holds the crop's key and the photo's is not
-- recoverable from it. A down-migration that silently relabels data is worse
-- than one that only moves the schema.

COMMENT ON COLUMN search_seeds.seed_kind IS NULL;
