-- Reverses 0023.
--
-- Drop the four new views first (their grant goes with them), then restore
-- v_person_hits and v_person_report_summary to 0016's exact text -- not just
-- "remove the WHERE", because CREATE OR REPLACE VIEW cannot drop a trailing
-- column, and a straight re-run of 0016's CREATE OR REPLACE is the only way
-- back to that exact projection. Copied verbatim from
-- migrations/0016_svc_contract_views.up.sql:123-253.
--
-- Dropping svc.v_person_score et al. breaks the proxy's reader for the score
-- surface, not this service -- the same versioned-contract asymmetry 0016's
-- down leg documents. Coordinated deploy, not a solo rollback.

REVOKE SELECT ON svc.v_person_score, svc.v_person_score_events,
                 svc.v_person_recommendations, svc.v_person_threat_context
  FROM imageshield_proxy_ro;

DROP VIEW IF EXISTS svc.v_person_threat_context;
DROP VIEW IF EXISTS svc.v_person_recommendations;
DROP VIEW IF EXISTS svc.v_person_score_events;
DROP VIEW IF EXISTS svc.v_person_score;

CREATE OR REPLACE VIEW svc.v_person_report_summary AS
SELECT
  s.user_ref                              AS person_ref,
  COALESCE(i.active_reports, 0)           AS active_reports,
  COALESCE(i.unresolved_matches, 0)       AS unresolved_matches,
  COALESCE(i.live_exposure_count, 0)      AS live_exposure_count,
  r.last_run_at,
  r.first_scan_completed_at,
  COALESCE(m.monitored_sources, 0)        AS monitored_sources
FROM subjects s
LEFT JOIN (
  SELECT
    user_ref,
    count(*) FILTER (WHERE status = 'new')::int                    AS active_reports,
    count(*) FILTER (WHERE status IN ('new', 'acknowledged'))::int  AS unresolved_matches,
    -- Both terminal user positions are excluded: 'dismissed_not_me' ("that is
    -- not my face") and 'authorised' ("that is me and it is fine"). Neither is
    -- exposure the user is still carrying, and a dead URL is not either.
    count(*) FILTER (
      WHERE url_alive
        AND status NOT IN ('dismissed_not_me', 'authorised')
    )::int                                                          AS live_exposure_count
  FROM infringements
  GROUP BY user_ref
) i ON i.user_ref = s.user_ref
LEFT JOIN (
  SELECT
    user_ref,
    max(completed_at) FILTER (WHERE status = 'completed') AS last_run_at,
    min(completed_at) FILTER (WHERE status = 'completed') AS first_scan_completed_at
  FROM search_runs
  GROUP BY user_ref
) r ON r.user_ref = s.user_ref
LEFT JOIN (
  SELECT sr.user_ref, count(DISTINCT p.provider_id)::int AS monitored_sources
  FROM search_runs sr
  CROSS JOIN LATERAL unnest(sr.providers_succeeded) AS p(provider_id)
  JOIN providers pr ON pr.provider_id = p.provider_id AND pr.enabled
  WHERE sr.status = 'completed'
  GROUP BY sr.user_ref
) m ON m.user_ref = s.user_ref;

-- NOT an OR REPLACE. Postgres refuses to drop trailing columns from a view
-- (measured: `cannot drop columns from view`), and this direction must remove
-- the three 0023 added (confirm_state, severity, decided_at) to reach 0016's
-- exact 18-column shape. A DROP+CREATE here does not reopen the concern
-- CLAUDE.md raises about the up leg -- that a DROP mid-deploy breaks a live
-- proxy view depending on the old shape -- because running this down file at
-- all is already the coordinated, proxy-breaking rollback 0016's own down leg
-- documents; there is no shape this statement could preserve that the four
-- new views' DROPs above have not already broken.
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
  rep.provider_score                      AS score
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
LEFT JOIN attributed_faces af ON af.face_id = seed.attributed_face_id;
