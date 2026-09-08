-- Where the subject's preview picture actually comes from, when the provider
-- gave us a page instead of an image.
--
-- The bug this closes, measured on real data 2026-09-07: of 12 hits from one
-- subject's first scan, ELEVEN had no preview at all. Every one of them had a
-- non-null image_url, so nothing looked broken -- but `image_url = page_url`
-- for all 12, because Google Vision's `pagesWithMatchingImages` entries carry
-- only `url` and `pageTitle`, and the adapter stored that page address in the
-- image column. fetcher.fetch_image then correctly refused it
-- (`not_an_image`, HTTP 400), so no triage ran, `triage -> best_face_bbox`
-- was never written, and `GET /v1/infringements/{id}/preview` answered 404
-- PREVIEW_UNAVAILABLE. The subject was asked "is this you?" about eleven
-- pictures they could not see, and their yes/not-me writes a real
-- confirm/reject decision (INVARIANTS #19).
--
-- WHY A NEW COLUMN RATHER THAN FIXING image_url IN PLACE.
-- `image_url` means "the image URL the provider gave us". It is what
-- `calibrate observe` and `calibrate replay` map provider responses against
-- (CLAUDE.md 7.2), so overwriting it with a value WE derived would make a
-- historical recalibration measure our derivation instead of the provider's
-- answer. The resolved URL is a different fact with a different provenance
-- and gets its own column. `image_url` is separately being corrected to hold
-- NULL rather than a page address for a page match -- that is the adapter's
-- bug, and it is not this column's job to paper over it.
--
-- Passes the #9 schema lint by suffix: `*_url` is explicitly allowed, and no
-- bytes are stored here or anywhere else. The page's HTML is read in memory by
-- the fetcher deployable and discarded within the request that read it; only
-- this address survives.

ALTER TABLE infringements
  ADD COLUMN preview_image_url TEXT;

COMMENT ON COLUMN infringements.preview_image_url IS
  'The image the PAGE publishes as its own preview (og:image, else '
  'twitter:image), resolved by confirm.og_image.page_preview_url from HTML '
  'read through the fetcher deployable. Set only when the provider keyed this '
  'hit on a page and therefore supplied no image address of its own; NULL '
  'means image_url was directly fetchable, or no preview could be resolved. '
  'DERIVED, never a provider value -- image_url remains the provider''s own '
  'answer so calibration replays against what they actually returned '
  '(CLAUDE.md 7.2). Read by the subject preview path, which prefers this over '
  'image_url.';
