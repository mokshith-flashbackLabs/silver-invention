-- svc.v_person_hits gains `preview_available`: can this hit actually show the
-- subject a picture?
--
-- WHY THE PROXY CANNOT WORK THIS OUT ITSELF. Their `card.ts` currently GUESSES
-- from confirm_state + severity, and the guess is wrong in the direction that
-- matters: on 2026-09-08, cards were sent with `preview_expected: true` for
-- hits whose preview then answered 404 PREVIEW_UNAVAILABLE. The fact lives in
-- two columns they are not granted and must not be granted -- `image_url` is
-- an infringing address and `review_tasks.triage` is the adjudication payload
-- -- so the only honest fix is for us to publish the derived boolean.
--
-- WHAT IT IS FOR. Owner's decision, 2026-09-08: "if no image then we should
-- not show". A page-keyed hit (Google Vision's `pagesWithMatchingImages`
-- returns a page and no image address) can neither be shown nor verified, and
-- 11 of one subject's 12 hits were that shape. Every one was presented as "is
-- this you?" and every one charged the score's D-03, so the subject lost 12
-- points to matches nothing could check and was asked twelve unanswerable
-- questions. This column is what lets their presentation and scoring exclude
-- exactly those, while keeping a CONFIRMED hit visible whether or not a
-- picture survives -- the subject already decided that one.
--
-- IT MIRRORS `preview/store.py:_TARGET_SQL` AND MUST MOVE WITH IT.
-- That module's `target()` is the render path: it selects
-- coalesce(preview_image_url, image_url) plus triage -> 'best_face_bbox' and
-- returns None if either is absent, which is precisely the condition below.
-- Two expressions of one fact is the drift risk this repo keeps finding, so
-- `tests/test_svc_preview_available.py` asserts the view and the render path
-- agree row by row. Change one, that test fails.
--
-- `jsonb_typeof(...) = 'object'`, NOT `IS NOT NULL`. `triage -> 'key'` yields
-- a JSONB null for a key present-but-null, and JSONB null is NOT SQL NULL --
-- so `IS NOT NULL` is TRUE for a bbox that is literally `null`. Four of that
-- subject's hits had exactly that shape, which is how a count of 7 renderable
-- previews was reported when the real number was 3.
--
-- CREATE OR REPLACE cannot add a column, so this is DROP + CREATE, and the
-- proxy's grant must therefore be re-issued below (0016's precedent; 0027 and
-- 0028 skipped the re-GRANT only because they added no column).

DROP VIEW IF EXISTS svc.v_person_hits;

CREATE VIEW svc.v_person_hits AS
WITH representative AS (
  SELECT DISTINCT ON (a.infringement_id)
    a.infringement_id,
    a.attestation_id,
    a.band,
    a.provider_score,
    a.last_run_id
  FROM attestations a
  ORDER BY
    a.infringement_id,
    CASE a.band WHEN 'auto_confirm' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
    a.provider_score DESC NULLS LAST,
    a.provider_id
),
latest_feedback AS (
  SELECT DISTINCT ON (infringement_id)
    infringement_id, signal, created_at
  FROM infringement_feedback
  ORDER BY infringement_id, created_at DESC, feedback_id
)
SELECT
  i.infringement_id                       AS hit_id,
  NULL::uuid                              AS report_id,
  i.user_ref                              AS person_ref,
  seed.source_object_ref                  AS source_photo_id,
  i.status                                AS hit_status,
  i.last_checked_at,
  rep.attestation_id                      AS match_id,
  c.source_domain,
  i.page_url                              AS host_page_url,
  af.bbox                                 AS face_bbox,
  NULL::text                              AS title,
  i.first_seen_at                         AS detected_at,
  rep.band                                AS match_status,
  fb.signal                               AS match_action,
  CASE
    WHEN i.url_alive = false THEN 'url_dead'
    ELSE 'open'
  END::text                               AS match_lifecycle,
  CASE
    WHEN i.url_alive = false THEN i.last_checked_at
    WHEN i.status IN ('dismissed_not_me', 'authorised', 'user_resolved') THEN fb.created_at
    ELSE NULL
  END                                     AS resolved_at,
  NULL::text                              AS resolution_note,
  (SELECT count(*)::int FROM attestations x
    WHERE x.infringement_id = i.infringement_id)  AS provider_count,
  rep.provider_score                      AS score,
  i.confirm_state,
  i.severity,
  i.confirm_decided_at                    AS decided_at,
  i.keyed_on,
  -- 0031, appended. Both halves are required because the render needs both: an
  -- address to fetch and a box to sharpen. Mirrors preview/store.py.
  (
    COALESCE(i.preview_image_url, i.image_url) IS NOT NULL
    AND jsonb_typeof(rt.triage -> 'best_face_bbox') = 'object'
  )                                       AS preview_available
FROM infringements i
JOIN content_urls c ON c.url_hash = i.url_hash
LEFT JOIN representative rep ON rep.infringement_id = i.infringement_id
LEFT JOIN latest_feedback fb ON fb.infringement_id = i.infringement_id
LEFT JOIN search_runs run ON run.run_id = rep.last_run_id
LEFT JOIN search_seeds seed ON seed.seed_id = run.seed_id
LEFT JOIN attributed_faces af ON af.face_id = seed.attributed_face_id
-- 0021's UNIQUE (infringement_id) makes this at most one row, so it cannot
-- fan out the result. LEFT because a hit that never triaged has no task row --
-- that is "no preview", not a missing hit.
LEFT JOIN review_tasks rt ON rt.infringement_id = i.infringement_id
WHERE i.confirm_state NOT IN ('quarantined', 'duplicate');

COMMENT ON VIEW svc.v_person_hits IS
  'One row per hit for the proxy''s report screen. 0031 adds '
  'preview_available: whether GET /v1/infringements/{id}/preview can actually '
  'render, i.e. an image address AND a face box both exist. The proxy uses it '
  'to avoid asking "is this you?" about a hit it cannot show, and to avoid '
  'charging its score for one. Mirrors preview/store.py:_TARGET_SQL -- the two '
  'must move together (tests/test_svc_preview_available.py).';

-- DROP dropped the grant with the view. Re-issue it, same as 0016.
GRANT SELECT ON svc.v_person_hits TO imageshield_proxy_ro;
