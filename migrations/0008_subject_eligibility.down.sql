-- Reverses 0008.
--
-- Dropping `subjects` loses the eligibility flags, which is a genuine loss of
-- safety-relevant state: after a down-migration nothing records that a given
-- user_ref is a minor. Reversibility is a CI requirement (build spec Phase 1
-- §5) so the pair exists, but reverting this in an environment holding real
-- minor enrolments re-opens discovery for them the moment the guard's table
-- disappears -- the route reads the table, and no table means no refusal.
--
-- The re-application path is safe: `up` backfills every user_ref in
-- `enrolments` and `search_seeds` as 'adult', which is WRONG for a minor. So
-- a down/up cycle in production must be followed by a proxy-driven
-- re-assertion of `subject_is_adult` for every enrolled subject. Recorded
-- here rather than in a runbook because this file is what an operator reads
-- immediately before running it.

ALTER TABLE search_seeds
  DROP CONSTRAINT search_seeds_subject_fk;

DROP TABLE subjects;
