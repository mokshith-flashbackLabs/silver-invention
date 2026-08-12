-- The `svc` contract views, and a fourth feedback signal.
--
-- WHAT WAS WRONG. PROXY_INTEGRATION.md §6 granted the proxy SELECT on
-- report.reports, report.report_hits and report.hit_feedback. None of those
-- tables has ever existed in this repo -- they come from an early SCHEMA.md
-- draft with a "report module" we never built. We built `infringements` and
-- `attestations` instead: different names, different shape. Meanwhile the
-- proxy's src/services/contract/readers.ts reads four views in an `svc` schema
-- that appeared in THEIR docs and in none of our nine build steps. Both sides
-- were building against a contract the other had not agreed to.
--
-- WHY THE VIEWS LIVE HERE AND NOT BEHIND HTTP. The deciding reason is not
-- single-source-of-truth for live_exposure_count, true though that is. It is
-- that the proxy's OWN views JOIN against ours -- v_person_enrolment_state
-- feeds their v_covered_persons, and their migration 0001 already does
-- LEFT JOIN profile.v_consent_eligibility. You cannot JOIN against HTTP, which
-- also rules out the option nobody listed: extending
-- GET /v1/search/infringements. That works for the report screen and fails for
-- coverage.
--
-- THE COST, ON THE RECORD. Two repos now share a database, and these four
-- views become a VERSIONED CONTRACT. Columns may be added freely; none may be
-- removed or retyped without a coordinated deploy. That is tighter coupling
-- than anything else in this architecture and it is being accepted
-- deliberately, not by default.
--
-- WHAT IS DELIBERATELY NOT GRANTED. SELECT on the four views, and nothing
-- else. No grant on any base table, no USAGE on `public`. A view's base-table
-- reads are checked against the view OWNER, so the proxy role can read exactly
-- the projection below and gets a permission error on
-- `SELECT * FROM public.enrolments` -- enforced by Postgres, not by anybody's
-- application logic. If the proxy can reach `enrolments` or `attributed_faces`
-- directly, the contract is not a contract.

-- ── Ask 2: the fourth feedback signal ─────────────────────────────────────
--
-- Their action vocabulary had one value with nowhere to go. 'not_infringement'
-- means "this is me, and it is authorised" -- my own post, a licensed use, a
-- photo I published myself. Mapping it to 'uncertain' would be wrong twice: it
-- records the opposite of what the user said, and 'uncertain' leaves
-- infringements.status unchanged so the hit never resolves. They dropped the
-- action from their client rather than record a false statement, which was the
-- right call.
--
-- 'authorised' TERMINATES the hit and is EXCLUDED from live_exposure_count
-- (see the view below). Without the exclusion, a user whose own licensed photo
-- is flagged keeps paying exposure points with no way to clear it -- a milder
-- version of the exact inversion these views exist to fix, where reporting
-- abuse made the number worse and dismissing a hit improved it.
--
-- 'uncertain' is untouched. Keeping it distinct is right: someone who looked
-- and could not tell has told us something.

ALTER TABLE infringement_feedback
  DROP CONSTRAINT infringement_feedback_signal_check;

ALTER TABLE infringement_feedback
  ADD CONSTRAINT infringement_feedback_signal_check
  CHECK (signal IN ('not_me', 'confirmed', 'uncertain', 'authorised'));

-- infringements.status has carried no CHECK since 0005. The vocabulary is now
-- a published contract (svc.v_person_hits.hit_status), so it stops being a
-- convention. 'url_dead' and 'withdrawn' are declared but unwritten in v1 --
-- the recheck loop sets url_alive and leaves status alone (see 0013), and
-- nothing withdraws.
ALTER TABLE infringements
  ADD CONSTRAINT infringements_status_valid CHECK (
    status IN ('new', 'acknowledged', 'dismissed_not_me', 'authorised',
               'url_dead', 'withdrawn')
  );

-- ── Ask 1: the schema, the role, and the four views ───────────────────────

CREATE SCHEMA IF NOT EXISTS svc;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'imageshield_proxy_ro') THEN
    CREATE ROLE imageshield_proxy_ro NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA svc TO imageshield_proxy_ro;

-- Column names are the PROXY'S, verbatim from their document, and are not
-- renamed to our internal vocabulary: infringements -> hits, attestations ->
-- matches. That is a legitimate use of a view. It also freezes the split
-- permanently, which is worth a deliberate choice while there is no
-- production data -- raised with them, not decided here.

-- ── v_person_enrolment_state ──────────────────────────────────────────────
-- A status and a timestamp. Never a vector, never an external_face_id --
-- INVARIANTS #14 holds, and this view is the one place it would be easy to
-- undo by adding "just one more column".
CREATE VIEW svc.v_person_enrolment_state AS
SELECT
  e.user_ref    AS person_ref,
  e.status,
  e.model_id,
  e.created_at  AS enrolled_at
FROM enrolments e
WHERE e.status = 'active';

-- ── v_person_report_summary ───────────────────────────────────────────────
-- Driven off `subjects`, not off `infringements`, so a person with zero
-- infringements appears WITH ZEROES rather than vanishing. A missing row and a
-- row of zeroes render very differently on a home screen, and the aggregate
-- form (count(*) FILTER ... GROUP BY user_ref) produces the wrong one.
--
-- The three aggregates are separate subqueries rather than one join over
-- infringements x search_runs: joining them first multiplies rows and every
-- count comes out wrong by a factor of the other table's cardinality.
--
-- last_run_at is filtered to status='completed' -- it means "the last time we
-- actually looked". A run refused at dispatch (#43) has a completed_at and
-- looked at nothing; reporting it here would be the same false reassurance
-- that invariant forbids. Refusals are readable on GET /v1/subjects.
--
-- monitored_sources counts providers that ACTUALLY RETURNED for this person
-- and are still enabled, not configured ones. "We monitor 2 sources" while one
-- has an open breaker is a false claim (CLAUDE.md §7.5).
CREATE VIEW svc.v_person_report_summary AS
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

-- ── v_person_hits ─────────────────────────────────────────────────────────
-- ONE ROW PER HIT, not per attestation. The same URL found by three providers
-- is one hit with three attestations (CLAUDE.md §7.4), so match_id / score /
-- match_status come from a single REPRESENTATIVE attestation chosen
-- deterministically: strongest band first, then highest raw score, then
-- provider_id to break ties. provider_count carries the agreement signal.
--
-- No score is combined across providers. Provider A's 0.92 and provider B's
-- 0.92 are different quantities (INVARIANTS #15c) and averaging them would
-- produce a plausible-looking number with no meaning.
--
-- DELIBERATELY ABSENT: image_url, thumbnail_url, evidence_image_url. The proxy
-- reached the same conclusion we did in 0005 -- the column stays on the row as
-- evidence and does not travel on a user-facing read. If a client ever needs
-- pixels it is a blurred face crop behind its own access-logged, authorised
-- path, never a widening of this view.
--
-- PERMANENTLY NULL, because there is no source for them here: report_id (no
-- reports table -- the hit IS the unit), title, resolution_note. Returned as
-- typed NULLs rather than omitted: a missing column breaks their reader, a NULL
-- does not.
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

-- ── v_person_liveness_attempts ────────────────────────────────────────────
-- The pre-check only, so the proxy can refuse a doomed session before it burns
-- a provider attempt. The window is not a free choice: it must be the SAME
-- predicate LIVENESS_MAX_ATTEMPTS_24H is enforced against in
-- liveness/store.py (_CHECK_SQL), or the proxy's pre-check and our refusal
-- disagree at the boundary and the client sees a 429 it was told it would not.
--
-- A person with no attempts in the window is ABSENT rather than a zero row.
-- Unlike the report summary this is safe: the reader is a rate-limit check, no
-- row means no attempts, and there is no home-screen number to render wrong.
CREATE VIEW svc.v_person_liveness_attempts AS
SELECT
  user_ref              AS person_ref,
  count(*)::int         AS attempts_24h,
  max(created_at)       AS last_attempt_at
FROM liveness_sessions
WHERE created_at > now() - interval '24 hours'
GROUP BY user_ref;

-- The whole grant. Four views, SELECT, nothing else.
GRANT SELECT ON
  svc.v_person_enrolment_state,
  svc.v_person_report_summary,
  svc.v_person_hits,
  svc.v_person_liveness_attempts
  TO imageshield_proxy_ro;
