-- Step 8, part one: the subject record, and the flag that stops discovery
-- running for a minor.
--
-- Minors enrol in v1 -- consent, guardianship and household seats all work.
-- Discovery must not run for them, and the reason is structural rather than
-- policy: discovery finds images resembling the seed, and nudify sites alter
-- real photos, so for an enrolled minor a SUCCESSFUL result is CSAM inside
-- this pipeline -- fetched by the crop fetcher, rendered to a parent, stored
-- in `infringements`, packaged by evidence export. There is no version of the
-- feature that works for a minor without that arising.
--
-- CSAM screening and reporting are deferred until the partner corpus
-- connects. Until they exist the correct behaviour is that NOTHING LOOKS, so
-- nothing is found and no mandatory-reporting obligation starts.
--
-- The flag lives HERE, in data this service owns, and not in the request.
-- Services cannot check age: they hold `user_ref` and no DOB, and that
-- boundary is correct (CLAUDE.md §3.2). But a per-request assertion from the
-- proxy means a proxy bug silently scans a minor, invisibly and with no
-- record. A refusal has to rest on data we hold.
--
-- This also closes a real gap: enrolments.user_ref, search_seeds.user_ref and
-- infringements.user_ref were unparented UUIDs with no subject row anywhere.

CREATE TABLE subjects (
  user_ref            UUID PRIMARY KEY,
  discovery_eligible  BOOLEAN NOT NULL,
  -- 'adult'                    -> eligible
  -- 'minor_discovery_deferred' -> ineligible until CSAM screening exists
  eligibility_reason  TEXT NOT NULL
                      CHECK (eligibility_reason IN
                             ('adult', 'minor_discovery_deferred')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- The pairing is the whole point of the table, so the database enforces it
  -- rather than trusting every writer: a row claiming 'adult' AND ineligible
  -- (or 'minor_discovery_deferred' AND eligible) is the exact corruption that
  -- would let a minor be scanned while the reason column reads reassuringly.
  CONSTRAINT subjects_reason_matches_flag CHECK (
    (discovery_eligible = true  AND eligibility_reason = 'adult') OR
    (discovery_eligible = false AND eligibility_reason = 'minor_discovery_deferred')
  )
);

-- Partial index on the ineligible rows: the guard's hot path is a PK lookup,
-- but "who is currently blocked from discovery" is the question an operator
-- and a future CSAM-readiness backfill both ask, and it is a tiny fraction of
-- the table.
CREATE INDEX subjects_ineligible_idx ON subjects (user_ref)
  WHERE discovery_eligible = false;

-- Everything enrolled so far predates minor support: MIN_ENROLMENT_AGE was 18
-- and enrolment refused anyone younger, so every existing subject is an adult
-- by construction. This is a statement about the old gate, not an assumption
-- about the population.
INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)
SELECT DISTINCT user_ref, true, 'adult' FROM enrolments
ON CONFLICT DO NOTHING;

INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)
SELECT DISTINCT user_ref, true, 'adult' FROM search_seeds
ON CONFLICT DO NOTHING;

-- A seed for a subject we have never heard of cannot be searched, so it must
-- not be creatable. This is the database half of the guard chain's step 1:
-- even if the route check were removed, an unparented seed fails here.
ALTER TABLE search_seeds
  ADD CONSTRAINT search_seeds_subject_fk
  FOREIGN KEY (user_ref) REFERENCES subjects(user_ref);

-- FOLLOW-UP, deliberately NOT done here: the same FK on `enrolments`.
-- Enrolment is what CREATES the subject row, in the same transaction as the
-- enrolments insert, so the ordering inside that transaction has to be
-- established and tested before a constraint depends on it. Adding it now
-- would make the FK's correctness contingent on statement order in a
-- function this migration cannot see.
