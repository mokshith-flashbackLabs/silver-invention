-- The user marks their own hit resolved: a fifth feedback signal, a seventh
-- infringements.status value, and the one-line view change each ripples
-- into. Design: docs/superpowers/specs/2026-08-29-user-resolved-hits-design.md.
--
-- WHY. The only path out of 'acknowledged' has been the weekly URL recheck
-- seeing the page come down (url_alive -> false, INVARIANTS/0013). A user who
-- gets a deepfake taken off a site on Monday still reads "unresolved" on
-- Friday if the recheck has not run yet -- the product telling them their own
-- effort did not count. The fix is to let the user say so themselves.
--
-- THE HONEST FRAME, which must survive into the proxy's copy: 'user_resolved'
-- is the USER'S ASSERTION that they have dealt with it, not our observation
-- that the content is gone. url_alive is UNTOUCHED by this migration and
-- stays a separate column written only by the recheck loop -- a
-- user-resolved hit keeps being probed, and if the page really does die
-- later, url_alive still flips and match_lifecycle still reports 'url_dead'.
-- The two facts are never merged into one.
--
-- NO TRANSITION GUARD HERE, deliberately. This repo records what the user
-- said from whatever state the hit is in when the signal arrives -- the same
-- shape as 'not_me' being recorded and not policed (search/feedback.py). "Only
-- from acknowledged" is a product rule enforced where the flow lives: the
-- backend, which is the only ingress (image_backend PATCH /v1/hits/{id},
-- 409 HIT_NOT_RESOLVABLE otherwise). A service-side guard here would be a
-- second copy of that rule, and a second copy is how the two disagree later.
--
-- REVERSAL IS FREE, with no special-casing anywhere in this migration or in
-- search/feedback.py. infringement_feedback is append-only -- a change of
-- mind is a second row, never an UPDATE -- and infringements.status is
-- last-write-wins, so sending 'confirmed' again after 'resolved' moves the
-- hit straight back to 'acknowledged'.
--
-- THE TWO VIEW EDITS ARE CREATE OR REPLACE, ONE SQL LINE EACH, EVERYTHING
-- ELSE BYTE-IDENTICAL to what is already live. v_person_report_summary below
-- is 0023's body (migrations/0023_svc_score_views.up.sql) with
-- live_exposure_count's exclusion list gaining 'user_resolved'.
-- v_person_hits below is 0027's body (migrations/0027_hits_keyed_on.up.sql,
-- itself 0023's with keyed_on appended) with resolved_at's CASE gaining the
-- same value. `unresolved_matches` (status IN ('new','acknowledged')) and
-- `active_reports` (status = 'new') need NO edit -- 'user_resolved' is
-- neither, so it falls out of both automatically. Neither view gains a
-- column, so neither needs DROP + CREATE, and CREATE OR REPLACE leaves the
-- proxy's existing grant on both untouched -- no re-GRANT below, same as
-- 0027's precedent.

-- ── the two CHECK constraints, widened ────────────────────────────────────
-- Widening never fails validation: every existing row already satisfies the
-- narrower set, which is a subset of this one. Plain ADD CONSTRAINT, same as
-- 0016 adding 'authorised' to this exact signal CHECK. NOT VALID is this
-- repo's tool for NARROWING one safely (0010, and this migration's own down
-- leg below) -- it has no work to do on a widen.

ALTER TABLE infringement_feedback
  DROP CONSTRAINT infringement_feedback_signal_check;

ALTER TABLE infringement_feedback
  ADD CONSTRAINT infringement_feedback_signal_check
  CHECK (signal IN ('not_me', 'confirmed', 'uncertain', 'authorised', 'resolved'));

-- 'url_dead' and 'withdrawn' stay declared-but-unwritten in v1, exactly as
-- 0016 left them -- this migration adds one value and touches no other.
ALTER TABLE infringements
  DROP CONSTRAINT infringements_status_valid;

ALTER TABLE infringements
  ADD CONSTRAINT infringements_status_valid CHECK (
    status IN ('new', 'acknowledged', 'dismissed_not_me', 'authorised',
               'url_dead', 'withdrawn', 'user_resolved')
  );

-- ── svc.v_person_report_summary, replaced ─────────────────────────────────
-- 0023's body verbatim; live_exposure_count's FILTER (and the comment above
-- it, kept accurate) gains 'user_resolved'. Nothing else on this view moves.
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
    -- Three terminal user positions are now excluded: 'dismissed_not_me'
    -- ("that is not my face"), 'authorised' ("that is me and it is fine"),
    -- and, as of 0028, 'user_resolved' ("I've dealt with it"). None of the
    -- three is exposure the user is still carrying, and a dead URL is not
    -- either.
    count(*) FILTER (
      WHERE url_alive
        AND status NOT IN ('dismissed_not_me', 'authorised', 'user_resolved')
    )::int                                                          AS live_exposure_count
  FROM infringements
  WHERE confirm_state NOT IN ('quarantined', 'duplicate')
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

-- ── svc.v_person_hits, replaced ───────────────────────────────────────────
-- 0027's body verbatim (itself 0023's with keyed_on appended); resolved_at's
-- CASE gains 'user_resolved' as a third terminal position that stamps the
-- user's own feedback timestamp, same as 'dismissed_not_me' and 'authorised'
-- already do. Every join, column, cast and the
-- confirm_state NOT IN ('quarantined','duplicate') filter are unchanged.
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
  -- 0028, appended: 'user_resolved' is the USER'S OWN ASSERTION that they
  -- have dealt with the hit -- not our observation that the page is gone.
  -- That stays url_alive's job alone (untouched by this migration; the
  -- recheck loop keeps probing a user-resolved hit exactly as before), so
  -- resolved_at can now be set by either fact independently.
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
