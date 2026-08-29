-- svc.v_person_hits gains `keyed_on`, appended.
--
-- WHY. The proxy now hands the subject the page URL once they have marked a
-- hit as abuse (their PATCH /v1/hits/{id} {action: 'infringement'} -> our
-- 'acknowledged'). `page_url` is the PAGE only when the provider returned a
-- backlink; with none, 0005 keys the infringement on the image URL and stores
-- that in page_url (`keyed_on = 'image_url'`), so the link the proxy would hand
-- over opens a raw image file -- possibly the subject's own intimate imagery,
-- full size and unblurred -- with nothing around it. The proxy cannot tell the
-- two apart from the view today. `keyed_on` lets it label or withhold that link.
--
-- ADDITIVE ONLY (PROXY_INTEGRATION.md §6, the versioned-contract rule):
-- CREATE OR REPLACE keeps every existing column's name, type and position and
-- appends one. The text below is 0023's verbatim
-- (migrations/0023_svc_score_views.up.sql) plus the one trailing column; the
-- row filter and every other column are unchanged. The proxy's grant survives
-- OR REPLACE, so no re-GRANT here.

CREATE OR REPLACE VIEW svc.v_person_hits AS
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
-- What the user last said. The table is append-only (a change of mind is a
-- second row), so "current position" is the newest row, not the only row.
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
  -- 'takedown_requested' and 'removed' are UNREACHABLE in v1 -- takedown is
  -- not built. The column is declared with all four values so their reader
  -- needs no change later; nobody should ship UI for a state the system cannot
  -- produce. Documented in SCHEMA.md and PROXY_INTEGRATION.md §6.
  CASE
    WHEN i.url_alive = false THEN 'url_dead'
    ELSE 'open'
  END::text                               AS match_lifecycle,
  CASE
    WHEN i.url_alive = false THEN i.last_checked_at
    WHEN i.status IN ('dismissed_not_me', 'authorised') THEN fb.created_at
    ELSE NULL
  END                                     AS resolved_at,
  NULL::text                              AS resolution_note,
  (SELECT count(*)::int FROM attestations x
    WHERE x.infringement_id = i.infringement_id)  AS provider_count,
  rep.provider_score                      AS score,
  i.confirm_state,
  i.severity,
  i.confirm_decided_at                    AS decided_at,
  -- 0027, appended: which URL keyed this infringement. 'page_url' when the
  -- provider returned a backlink; 'image_url' when it did not, in which case
  -- host_page_url above IS the image's own address, not a page.
  i.keyed_on
FROM infringements i
JOIN content_urls c ON c.url_hash = i.url_hash
-- LEFT throughout: an infringement with no attestation cannot currently exist
-- (the write path creates both), but a hit disappearing from a user's report
-- because of a provenance gap is the wrong failure mode for this data.
LEFT JOIN representative rep ON rep.infringement_id = i.infringement_id
LEFT JOIN latest_feedback fb ON fb.infringement_id = i.infringement_id
-- Provenance chain to the seed and the face that produced it: attestation ->
-- run -> seed -> attributed face. face_bbox is NULL for seeds that predate
-- attribution and for the enrolment ReferenceImage seed (0014).
LEFT JOIN search_runs run ON run.run_id = rep.last_run_id
LEFT JOIN search_seeds seed ON seed.seed_id = run.seed_id
LEFT JOIN attributed_faces af ON af.face_id = seed.attributed_face_id
WHERE i.confirm_state NOT IN ('quarantined', 'duplicate');
