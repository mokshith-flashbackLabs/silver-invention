-- Reverses 0028.
--
-- COORDINATED DEPLOY, same as every other change to these two views and to
-- infringements.status: the backend sends `signal: 'resolved'` on this line
-- once its own PATCH /v1/hits/{id} ships it, and a database reverted past
-- 0028 refuses that signal with a CHECK violation (surfacing to the backend
-- as its 502 ADMIN_UPSTREAM_ERROR shape). Reverting this migration also makes
-- every 'user_resolved' hit re-appear as 'acknowledged' -- i.e. unresolved --
-- which is the correct, honest consequence of removing the concept, not a
-- side effect to work around.
--
-- ── infringements.status: reassign forward, THEN narrow ──────────────────
-- A CHECK cannot be narrowed while a row violates it -- ADD CONSTRAINT scans
-- and validates every existing row unless NOT VALID is given (see the
-- feedback signal below for the case where that matters). 'acknowledged' is
-- not a guess about where a user_resolved row belongs: it is the ONLY state
-- 'user_resolved' is ever reached from (the backend's transition guard,
-- since this repo enforces none -- see 0028's up leg), and it is exactly the
-- state a user already lands back in today by sending 'confirmed' again.
-- This UPDATE does not invent a transition; it applies the one that already
-- exists, in bulk, to every row the narrower CHECK below would otherwise
-- reject.
UPDATE infringements SET status = 'acknowledged' WHERE status = 'user_resolved';

ALTER TABLE infringements
  DROP CONSTRAINT infringements_status_valid;

ALTER TABLE infringements
  ADD CONSTRAINT infringements_status_valid CHECK (
    status IN ('new', 'acknowledged', 'dismissed_not_me', 'authorised',
               'url_dead', 'withdrawn')
  );

-- ── infringement_feedback.signal: NOT VALID, not DELETE ───────────────────
-- The same narrowing problem, and this table has no honest reassignment to
-- fall back on: it is an append-only EVENT LOG (0012), not a current-position
-- column, so there is no "state this row already means" to move a 'resolved'
-- row to the way 'acknowledged' works for infringements.status above.
-- Rewriting it to a signal the user never sent would falsify the record;
-- deleting it would erase that a real person, about their own abuse case,
-- said this happened.
--
-- This exact question was already decided once, in this exact migration
-- pair's ancestor: 0016's down leg reverts 'authorised' the same way and
-- says why in full -- "the only way to make it succeed is DELETEing a user's
-- recorded position on their own report without telling anyone", rejected.
-- NOT VALID is Postgres's precise tool for "grandfather what is already
-- here, enforce on every future INSERT" (also 0010's precedent), so a
-- 'resolved' row written before this rollback stays exactly as the user left
-- it and simply cannot be written again until 0028 is reapplied.
ALTER TABLE infringement_feedback
  DROP CONSTRAINT infringement_feedback_signal_check;

ALTER TABLE infringement_feedback
  ADD CONSTRAINT infringement_feedback_signal_check
  CHECK (signal IN ('not_me', 'confirmed', 'uncertain', 'authorised')) NOT VALID;

-- ── the two views, restored to their pre-0028 text exactly ────────────────
-- Neither 0028 edit added a column, so CREATE OR REPLACE is enough in this
-- direction too -- no DROP + CREATE, no ACL to restore, unlike 0023's or
-- 0027's down legs (which did add/remove trailing columns). This is 0023's
-- v_person_report_summary body and 0027's v_person_hits body, unchanged.

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
