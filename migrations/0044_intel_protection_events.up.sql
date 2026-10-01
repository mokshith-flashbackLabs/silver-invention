-- 0044 -- likeness intel, step 4: protection credits
-- (spec docs/superpowers/specs/2026-09-27-likeness-intel-design.md sections 3.7 and 4.8).
--
-- An approved protection_event proposal becomes a protection_events row in the decision's own
-- transaction (intel/decisions.py), the only inserter. proposal_id is NOT NULL: every credit has
-- citations, by construction, and there is no hand-created credit. A credit lapses at review_by
-- unless a renewal an operator approves continues it (renews_event_id). The failure mode is less
-- reassurance, never stale reassurance.
--
-- Deploy order: services first on the way up, the backend first on the way down. This migration
-- and the backend's engine term ship before any protection approval.

CREATE TABLE protection_events (
  event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title           TEXT NOT NULL CHECK (title <> ''),
  -- Control room only. An approval inserts '': a protection proposal carries only a title.
  body            TEXT NOT NULL DEFAULT '',
  -- SMALLINT like threat_events.severity, so the view's magnitude column stays smallint.
  strength        SMALLINT NOT NULL CHECK (strength BETWEEN 1 AND 5),
  tags            TEXT[] NOT NULL DEFAULT '{}'
                  CONSTRAINT protection_events_tags_well_formed CHECK (intel_tags_well_formed(tags)),
  is_global       BOOLEAN NOT NULL DEFAULT false,
  starts_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  review_by       TIMESTAMPTZ NOT NULL,
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retracted')),
  proposal_id     UUID NOT NULL
                  CONSTRAINT protection_events_proposal_id_key UNIQUE
                  CONSTRAINT protection_events_proposal_id_fkey
                    REFERENCES intel_proposals (proposal_id),
  renews_event_id UUID
                  CONSTRAINT protection_events_renews_event_id_key UNIQUE
                  CONSTRAINT protection_events_renews_event_id_fkey
                    REFERENCES protection_events (event_id),
  created_by      TEXT NOT NULL CHECK (created_by <> ''),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  retracted_by    TEXT,
  retracted_at    TIMESTAMPTZ,
  retract_reason  TEXT,
  -- Exactly one scope: named tags, or everyone.
  CONSTRAINT protection_events_reaches_someone CHECK (is_global OR cardinality(tags) > 0),
  CONSTRAINT protection_events_one_scope CHECK (NOT (is_global AND cardinality(tags) > 0)),
  CONSTRAINT protection_events_review_window
    CHECK (review_by > starts_at AND review_by <= starts_at + interval '366 days'),
  -- Two-sided: an active credit carries no retraction fields, a retracted one all three.
  CONSTRAINT protection_events_retraction_shape CHECK (
    (status = 'active' AND retracted_by IS NULL AND retracted_at IS NULL
       AND retract_reason IS NULL)
    OR (status = 'retracted' AND retracted_by IS NOT NULL AND retracted_at IS NOT NULL
       AND retract_reason IS NOT NULL))
);
CREATE INDEX protection_events_active_idx ON protection_events (review_by) WHERE status = 'active';

-- Approving inserts a credit and locks the one a renewal continues (intel/decisions.py);
-- retracting updates one (intel/protection_store.py). Both run as intel_rw. No DELETE, ever.
GRANT SELECT, INSERT, UPDATE ON protection_events TO intel_rw;

-- One queued or running renewal_check per credit (spec section 4.8). The single-worker
-- assumption is written down (section 3.4); this keeps an overlapping task during a rolling
-- deploy harmless, as intel_runs_one_open_per_source does for sources. intel_runs.kind has
-- allowed renewal_check since 0039, so no CHECK changes here.
CREATE UNIQUE INDEX intel_runs_one_open_renewal ON intel_runs ((request ->> 'event_id'))
  WHERE kind = 'renewal_check' AND status IN ('queued', 'running');

-- The tenth contract view, now with the protection half. CREATE OR REPLACE rather than DROP and
-- CREATE: Postgres refuses a replacement that renames, reorders or retypes any column, so the
-- contract's ten columns and types are held by the database itself, and the grant survives. It
-- is re-issued below anyway. The threat half is byte-identical to 0042's
-- (tests/test_intel_schema.py asserts it).
CREATE OR REPLACE VIEW svc.v_active_scoped_events AS
SELECT event_id,
       'threat'::text AS direction,
       kind,
       title,
       body,
       severity       AS magnitude,
       tags,
       is_global,
       starts_at,
       expires_at     AS ends_at
  FROM threat_events
 WHERE status = 'active' AND starts_at <= now() AND expires_at > now()
   AND cardinality(tags) > 0
UNION ALL
SELECT event_id,
       'protection'::text,
       'protection'::text,
       title,
       body,
       strength,
       tags,
       is_global,
       starts_at,
       review_by
  FROM protection_events
 WHERE status = 'active' AND starts_at <= now() AND review_by > now();

GRANT SELECT ON svc.v_active_scoped_events TO imageshield_proxy_ro;
