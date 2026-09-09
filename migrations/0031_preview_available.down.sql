-- Reverses 0031: svc.v_person_hits back to 0028's body, without
-- preview_available and without the review_tasks join.
--
-- DROP + CREATE again, so the proxy grant is re-issued at the end. A proxy
-- still reading preview_available after this runs gets 42703
-- undefined_column, which is why the coordinated order is proxy-first on the
-- way back down -- the mirror of services-first on the way up.

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
  i.keyed_on
FROM infringements i
JOIN content_urls c ON c.url_hash = i.url_hash
LEFT JOIN representative rep ON rep.infringement_id = i.infringement_id
LEFT JOIN latest_feedback fb ON fb.infringement_id = i.infringement_id
LEFT JOIN search_runs run ON run.run_id = rep.last_run_id
LEFT JOIN search_seeds seed ON seed.seed_id = run.seed_id
LEFT JOIN attributed_faces af ON af.face_id = seed.attributed_face_id
WHERE i.confirm_state NOT IN ('quarantined', 'duplicate');

GRANT SELECT ON svc.v_person_hits TO imageshield_proxy_ro;
