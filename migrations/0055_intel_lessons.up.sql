-- 0055 -- likeness intel: lessons and published evidence for the weekly report (spec
-- docs/superpowers/specs/2026-10-15-intel-lessons-and-evidence-design.md section 8).
--
-- A seventh proposal kind, lesson, and a run kind, lesson_draft. intel_lessons holds every approved
-- lesson: an event's (drafted with it) or a general one. The evidence an operator approved on is
-- SNAPSHOTTED at the decision (intel_published_owners / intel_published_evidence): the independence
-- count is Python (intel/corroboration.py) and cannot be a view expression. Three read-only views
-- publish lessons, evidence and the week's research totals to the backend. Events, never people.

-- The two intel_proposals CHECKs, by the names 0054 gave them.
ALTER TABLE intel_proposals DROP CONSTRAINT intel_proposals_kind_check;
ALTER TABLE intel_proposals ADD CONSTRAINT intel_proposals_kind_check
  CHECK (kind IN ('weight_change', 'threat_event', 'protection_event', 'weight_suggestion',
                  'coverage_gap', 'quiz_question', 'lesson'));
ALTER TABLE intel_proposals DROP CONSTRAINT intel_proposals_decided_check;
ALTER TABLE intel_proposals ADD CONSTRAINT intel_proposals_decided_check
  CHECK (NOT (status IN ('approved', 'applied')
              AND kind IN ('weight_change', 'threat_event', 'protection_event', 'quiz_question',
                           'lesson'))
         OR decided IS NOT NULL);

ALTER TABLE intel_runs DROP CONSTRAINT intel_runs_kind_valid;
ALTER TABLE intel_runs ADD CONSTRAINT intel_runs_kind_valid
  CHECK (kind IN ('source_check', 'discovery', 'adhoc_url', 'weight_suggestion', 'renewal_check',
                  'gap_regenerate', 'source_proposal', 'source_validation', 'lesson_draft'));

CREATE TABLE intel_lessons (
  lesson_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  proposal_id   UUID NOT NULL REFERENCES intel_proposals (proposal_id),
  -- A threat_events or protection_events id (two tables, so no FK), or NULL for a general lesson.
  event_id      UUID,
  kind          TEXT NOT NULL CHECK (kind IN ('threat', 'protection', 'general')),
  topic         TEXT CHECK (topic ~ '^[a-z][a-z0-9_]{1,59}$'),
  title         TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 60),
  intro         TEXT NOT NULL CHECK (length(intro) BETWEEN 1 AND 400),
  sections      JSONB NOT NULL CHECK (jsonb_typeof(sections) = 'array'),
  link_url      TEXT CHECK (link_url IS NULL OR link_url LIKE 'https://%'),
  link_label    TEXT CHECK (link_label IS NULL OR length(link_label) BETWEEN 1 AND 40),
  tags          TEXT[] NOT NULL DEFAULT '{}' CHECK (intel_tags_well_formed(tags)),
  is_global     BOOLEAN NOT NULL DEFAULT false,
  approved_by   TEXT NOT NULL CHECK (approved_by <> ''),
  approved_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  retired_by    TEXT,
  retired_at    TIMESTAMPTZ,
  retire_reason TEXT,
  CHECK ((kind = 'general') = (event_id IS NULL)),
  CHECK ((kind = 'general') = (topic IS NOT NULL)),
  CHECK ((link_url IS NULL) = (link_label IS NULL)),
  CHECK ((retired_at IS NULL) = (retired_by IS NULL)),
  CHECK ((retired_at IS NULL) = (retire_reason IS NULL))
);
CREATE UNIQUE INDEX intel_lessons_one_live_per_event ON intel_lessons (event_id)
  WHERE event_id IS NOT NULL AND retired_at IS NULL;
CREATE UNIQUE INDEX intel_lessons_one_live_per_topic ON intel_lessons (topic)
  WHERE topic IS NOT NULL AND retired_at IS NULL;

CREATE TABLE intel_published_owners (
  owner_kind          TEXT NOT NULL CHECK (owner_kind IN ('event', 'lesson')),
  owner_id            UUID NOT NULL,
  proposal_id         UUID NOT NULL REFERENCES intel_proposals (proposal_id),
  independent_sources INT NOT NULL CHECK (independent_sources >= 0),
  approved_at         TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (owner_kind, owner_id)
);

CREATE TABLE intel_published_evidence (
  owner_kind TEXT NOT NULL,
  owner_id   UUID NOT NULL,
  signal_id  UUID NOT NULL REFERENCES intel_signals (signal_id),
  excerpt_id UUID NOT NULL REFERENCES intel_excerpts (excerpt_id),
  PRIMARY KEY (owner_kind, owner_id, excerpt_id),
  FOREIGN KEY (owner_kind, owner_id) REFERENCES intel_published_owners (owner_kind, owner_id)
);
CREATE INDEX intel_published_evidence_signal_idx ON intel_published_evidence (signal_id);

-- Each new table its own grant (the migration-grant trap). No DELETE, ever.
GRANT SELECT, INSERT, UPDATE ON intel_lessons TO intel_rw;
GRANT SELECT, INSERT, UPDATE ON intel_published_owners TO intel_rw;
GRANT SELECT, INSERT ON intel_published_evidence TO intel_rw;

-- Evidence: only signals still active. A retracted signal's rows leave the view at once; the
-- retraction recomputes independent_sources (intel/evidence_store.py). link_url is a vetted
-- document's URL and NULL for every web-search find (spec D3).
CREATE VIEW svc.v_evidence AS
SELECT o.owner_kind,
       o.owner_id,
       d.document_id,
       d.publisher_domain,
       d.title                                              AS document_title,
       d.published_at,
       x.excerpt_id,
       x.quote_text                                         AS quote,
       CASE WHEN d.trust = 'listed' THEN d.final_url END    AS link_url,
       o.independent_sources,
       o.approved_at
  FROM intel_published_owners o
  JOIN intel_published_evidence pe ON pe.owner_kind = o.owner_kind AND pe.owner_id = o.owner_id
  JOIN intel_signals s ON s.signal_id = pe.signal_id AND s.status = 'active'
  JOIN intel_excerpts x ON x.excerpt_id = pe.excerpt_id
  JOIN intel_documents d ON d.document_id = s.document_id;

-- Live lessons only: not retired, and an event's lesson only while its event is live.
CREATE VIEW svc.v_lessons AS
SELECT l.lesson_id, l.event_id, l.kind, l.topic, l.title, l.intro, l.sections, l.link_url,
       l.link_label, l.tags, l.is_global, l.approved_at
  FROM intel_lessons l
 WHERE l.retired_at IS NULL
   AND (l.event_id IS NULL
        OR EXISTS (SELECT 1 FROM threat_events t
                    WHERE t.event_id = l.event_id AND t.status = 'active'
                      AND t.starts_at <= now() AND t.expires_at > now())
        OR EXISTS (SELECT 1 FROM protection_events p
                    WHERE p.event_id = l.event_id AND p.status = 'active'
                      AND p.starts_at <= now() AND p.review_by > now()));

-- Counts per Mon-Sun week (UTC). No title, no URL, no operator.
CREATE VIEW svc.v_intel_weekly_totals AS
WITH docs AS (
  SELECT date_trunc('week', fetched_at AT TIME ZONE 'UTC')::date AS week_start,
         count(*)::int AS documents_read,
         count(DISTINCT publisher_domain)::int AS publishers
    FROM intel_documents GROUP BY 1
), risks AS (
  SELECT date_trunc('week', created_at AT TIME ZONE 'UTC')::date AS week_start,
         count(*)::int AS risks_found
    FROM intel_signals WHERE status = 'active' AND direction = 'risk_up' GROUP BY 1
), proposed AS (
  SELECT date_trunc('week', created_at AT TIME ZONE 'UTC')::date AS week_start,
         count(*)::int AS proposed
    FROM intel_proposals WHERE kind IN ('threat_event', 'protection_event', 'lesson') GROUP BY 1
), decided AS (
  SELECT date_trunc('week', decided_at AT TIME ZONE 'UTC')::date AS week_start,
         count(*) FILTER (WHERE status IN ('approved', 'applied'))::int AS approved,
         count(*) FILTER (WHERE status = 'rejected')::int AS rejected
    FROM intel_proposals
   WHERE kind IN ('threat_event', 'protection_event', 'lesson') AND decided_at IS NOT NULL
   GROUP BY 1
), weeks AS (
  SELECT week_start FROM docs UNION SELECT week_start FROM risks
  UNION SELECT week_start FROM proposed UNION SELECT week_start FROM decided
)
SELECT w.week_start,
       coalesce(docs.documents_read, 0) AS documents_read,
       coalesce(docs.publishers, 0)     AS publishers,
       coalesce(risks.risks_found, 0)   AS risks_found,
       coalesce(proposed.proposed, 0)   AS proposed,
       coalesce(decided.approved, 0)    AS approved,
       coalesce(decided.rejected, 0)    AS rejected
  FROM weeks w
  LEFT JOIN docs ON docs.week_start = w.week_start
  LEFT JOIN risks ON risks.week_start = w.week_start
  LEFT JOIN proposed ON proposed.week_start = w.week_start
  LEFT JOIN decided ON decided.week_start = w.week_start;

GRANT SELECT ON svc.v_evidence, svc.v_lessons, svc.v_intel_weekly_totals TO imageshield_proxy_ro;
