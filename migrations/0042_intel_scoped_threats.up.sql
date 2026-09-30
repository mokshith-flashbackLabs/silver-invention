-- 0042 -- likeness intel, step 3: threat events aimed at exposure tags
-- (spec docs/superpowers/specs/2026-09-27-likeness-intel-design.md section 3.7).
--
-- An approved threat_event proposal becomes a threat_events row in the decision's own
-- transaction (intel/decisions.py), carrying the tags an operator approved and the proposal
-- it came from. Services never match a tag to a person: svc.v_active_scoped_events
-- publishes the EVENT, and the backend matches its own quiz answers against the tags.
--
-- Deploy order: services first on the way up, the backend first on the way down. The
-- backend declares the view optional; this service's /readyz requires it.

ALTER TABLE threat_events
  ADD COLUMN tags TEXT[] NOT NULL DEFAULT '{}'
    CONSTRAINT threat_events_tags_well_formed CHECK (intel_tags_well_formed(tags)),
  -- Nullable: operators still create events by hand. UNIQUE: one event per approval.
  ADD COLUMN proposal_id UUID
    CONSTRAINT threat_events_proposal_id_key UNIQUE
    CONSTRAINT threat_events_proposal_id_fkey REFERENCES intel_proposals (proposal_id);

-- 0022 wrote the relevance CHECK unnamed, so Postgres chose its name. It is found by its
-- DEFINITION, never assumed to be threat_events_check1. Whitespace, parentheses and a
-- NOT VALID suffix (a previous 0042 down leaves one) are ignored in the comparison.
DO $$
DECLARE
  relevance text;
BEGIN
  SELECT c.conname INTO STRICT relevance
    FROM pg_constraint c
   WHERE c.conrelid = 'public.threat_events'::regclass
     AND c.contype = 'c'
     AND regexp_replace(regexp_replace(pg_get_constraintdef(c.oid), ' NOT VALID$', ''),
                        '[[:space:]()]', '', 'g') = 'CHECKis_globalORcardinalitydomains>0';
  EXECUTE format('ALTER TABLE threat_events DROP CONSTRAINT %I', relevance);
EXCEPTION
  WHEN no_data_found THEN
    RAISE EXCEPTION '0042: threat_events has no CHECK (is_global OR cardinality(domains) > 0)';
  WHEN too_many_rows THEN
    RAISE EXCEPTION '0042: threat_events has more than one relevance CHECK';
END
$$;

-- NOT VALID, then validated. A row that satisfied the old CHECK satisfies this weaker one,
-- so on a database that never ran a 0042 down this validates at once. The one exception is
-- a row an earlier 0042 down grandfathered: a retracted tag-only event whose tags left with
-- the column. It keeps this constraint unvalidated rather than failing the up.
ALTER TABLE threat_events ADD CONSTRAINT threat_events_relevant
  CHECK (is_global OR cardinality(domains) > 0 OR cardinality(tags) > 0) NOT VALID;

DO $$
BEGIN
  ALTER TABLE threat_events VALIDATE CONSTRAINT threat_events_relevant;
EXCEPTION
  WHEN check_violation THEN
    RAISE NOTICE '0042: rows an earlier 0042 down grandfathered keep threat_events_relevant unvalidated';
END
$$;

-- Approving a threat proposal inserts the event in the decision's own transaction. SELECT
-- also serves the prompt's live events and the proposal read's related events. No UPDATE
-- and no DELETE: retraction stays the threat store's write (score_rw, 0022). The follow-up
-- migration that drops score_rw must re-home that grant first (SCHEMA.md open items).
GRANT SELECT, INSERT ON threat_events TO intel_rw;

-- The tenth contract view. Events, never people: the backend matches tags against its own
-- quiz answers. The threat half only; step 4 re-creates it as a UNION with the protection
-- half. Domain and global threats keep reaching people through v_person_threat_context; an
-- event scoped both ways appears on both, and the backend dedupes by event_id.
CREATE VIEW svc.v_active_scoped_events AS
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
   AND cardinality(tags) > 0;

GRANT SELECT ON svc.v_active_scoped_events TO imageshield_proxy_ro;
