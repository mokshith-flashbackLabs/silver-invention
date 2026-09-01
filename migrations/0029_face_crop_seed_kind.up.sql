-- A fourth seed_kind: 'face_crop'.
--
-- On a photo with two or more detected faces, each attributed subject's seed
-- is now a crop of their own face rather than the whole photo (spec
-- 2026-08-31). The reason is consent, not detection: while the full photo is
-- the seed, a person in frame who never consented -- a household member with
-- monitoring off, one who has not enrolled, a passer-by -- has their face sent
-- to Hive and Google on every scan cycle, indefinitely. The face-level seed
-- gate stops a stranger becoming a monitored SUBJECT. It says nothing about
-- their face being TRANSMITTED.
--
-- There is no CHECK on seed_kind to widen -- it is TEXT with a vocabulary
-- carried in a comment (0001:69), and that comment is now three-quarters true,
-- which is worse than absent. This migration is the vocabulary, in the one
-- place \d+ will show it.
--
-- Reusing 'user_supplied' was the alternative and needed no migration at all.
-- It was rejected because the corpus stops being legible: nothing then
-- separates "the photo they gave us" from "a region we cut out of it", and
-- that is the first question anyone asks when calibration looks odd. One
-- COMMENT buys a GROUP BY that answers it.

COMMENT ON COLUMN search_seeds.seed_kind IS
  'What kind of image this seed is. One of: '
  '''enrolment'' (the liveness ReferenceImage), '
  '''user_supplied'' (a photo the user uploaded, seeded whole -- the answer for '
  'a single-face photo and for a caller that minted no crop targets), '
  '''face_crop'' (one subject''s face cut out of a group photo, so the other '
  'people in it are not transmitted to a search provider -- spec 2026-08-31), '
  '''public_profile'' (specified, not built). '
  'TEXT rather than an enum on purpose: adding a kind must not need a '
  'type change and a lock.';
