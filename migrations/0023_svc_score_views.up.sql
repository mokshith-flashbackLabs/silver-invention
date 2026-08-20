-- svc contract views v2: four new score/threat views, plus the confirm
-- pipeline's quarantine/duplicate exclusion on the two existing views that
-- aggregate `infringements`.
--
-- WHY THE FOUR NEW VIEWS. Task 15 (0022) gave this repo protection_scores,
-- score_events, recommendations and threat_event_matches/threat_events. None
-- of it is reachable by the proxy yet, and CLAUDE.md §3 is unambiguous: all
-- user-facing reads go through the four `svc` views and nothing else. A score
-- the proxy cannot read is a score no user ever sees. These four are pure
-- additions -- new views, new grants -- so nothing about the existing
-- contract moves.
--
-- WHY v_person_hits AND v_person_report_summary CHANGE TOO. 0021 gave every
-- infringement a `confirm_state`, and two of its six values are user-facing
-- non-starters: 'quarantined' (a human pulled it for review-queue reasons a
-- user must never see mid-decision) and 'duplicate' (the same picture the
-- user already answered, now living under a second URL). Before this
-- migration both states are invisible to the *application* but still counted
-- and displayed by these two views, because they were built before
-- confirm_state existed. That is a live bug in the contract, not a new
-- feature: a quarantined hit reaching a user surface is exactly the harm the
-- review queue exists to prevent (INVARIANTS #19), and a duplicate inflates
-- active_reports/unresolved_matches/live_exposure_count for something the
-- user has a single answer for, not two.
--
-- THE OR REPLACE DISCIPLINE. Postgres requires a `CREATE OR REPLACE VIEW` to
-- keep every existing column's name, type and position -- only appending is
-- allowed. Both replaced views below start from 0016's text verbatim
-- (migrations/0016_svc_contract_views.up.sql:123-253); v_person_hits appends
-- three columns at the end of the select list and both gain a row filter.
-- Nothing else about either view's shape changes. The down leg restores
-- 0016's text exactly, because a down that cannot reproduce the pre-0023
-- projection breaks /readyz on rollback (CLAUDE.md's versioned-contract
-- rule, same one 0016 itself states).

-- ── v_person_hits, replaced ───────────────────────────────────────────────
-- Appends confirm_state, severity and decided_at (confirm_decided_at) so the
-- proxy can eventually surface a human decision on a hit; excludes
-- 'quarantined' and 'duplicate' rows from the projection entirely, not just
-- from a count -- a quarantined hit must not reach a user surface at all,
-- and a duplicate is the same picture already answered once.
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
  i.confirm_decided_at                    AS decided_at
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

-- ── v_person_report_summary, replaced ─────────────────────────────────────
-- Only the inner `infringements` aggregate changes: a WHERE that removes
-- quarantined and duplicate rows from the three counts before they are
-- computed, not after. Everything else -- the driving table, the other two
-- subqueries, the outer projection -- is 0016's text unchanged.
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

-- ── four new views ─────────────────────────────────────────────────────────
-- All four are straight projections off task 15's tables (0022), same
-- translation idiom as 0016: our `user_ref` becomes their `person_ref`, and
-- nothing else is renamed. All user-safe -- no vector, no external face id, no
-- image byte, no phone number.

CREATE VIEW svc.v_person_score AS
SELECT user_ref AS person_ref, score, components, config_version, computed_at
FROM protection_scores;

-- Append-only journal, read-only here as everywhere else. `cause_ref` is an
-- opaque provenance id (an infringement id, a run id, a threat event id --
-- whatever score/store.py stamped it with), never a URL and never rendered as
-- one.
CREATE VIEW svc.v_person_score_events AS
SELECT score_event_id, user_ref AS person_ref, delta, component, cause_kind,
       cause_ref, score_after, created_at
FROM score_events;

CREATE VIEW svc.v_person_recommendations AS
SELECT rec_id, user_ref AS person_ref, kind, params, status, source_event_id,
       created_at, completed_at, expires_at
FROM recommendations;

-- Only ACTIVE, UNEXPIRED threat context -- an expired or retracted event is
-- not something to show a user, and `threat_events.status` already carries
-- 'draft' and 'retracted' states that must never reach this projection.
CREATE VIEW svc.v_person_threat_context AS
SELECT m.user_ref AS person_ref, e.event_id, e.kind, e.title, e.body, e.severity,
       e.starts_at, e.expires_at
FROM threat_event_matches m
JOIN threat_events e USING (event_id)
WHERE e.status = 'active' AND e.expires_at > now();

GRANT SELECT ON svc.v_person_score, svc.v_person_score_events,
                svc.v_person_recommendations, svc.v_person_threat_context
  TO imageshield_proxy_ro;
