-- Preserve what the adaptive cadence would have chosen, now that it no longer
-- decides dispatch.
--
-- Owner's decision, 2026-09-09: every seed is scanned EVERY SUNDAY, tiers
-- ignored. `search/cadence.py` is NOT deleted and NOT unwired -- it still
-- computes the adaptive tier (weekly -> fortnightly after 8 empty scans ->
-- monthly after 20, promoting on any non-empty scan) on every completed run.
-- What changes is that `next_scan_after` now holds the coming SUNDAY rather
-- than the tier's interval.
--
-- So the two columns mean different things and must not both claim to be the
-- schedule:
--   next_scan_after      -- what WILL happen: the coming Sunday. The proxy's
--                           search sweep reads its own mirror of this to
--                           enqueue, and INVARIANTS #42 exists so a subject is
--                           told their real cadence -- a tier-derived date
--                           nothing acts on is precisely the lie it forbids.
--   tier_next_scan_after -- what the adaptive policy WOULD have chosen.
--                           Advisory, and the reason this migration exists:
--                           §7.8 puts adaptive cadence at a 4-10x cost
--                           reduction, and the moment we stop honouring it
--                           that claim becomes unmeasurable. Recording it
--                           keeps the saving forgone a query rather than a
--                           guess.
--
-- `scan_tier` keeps its meaning and keeps being written; it is advisory too.
-- Restoring tiering is one expression in `cadence.update_for` -- see
-- docs/superpowers/specs/2026-09-09-weekly-scan-cadence-design.md.
--
-- NO SCHEDULER TABLE HERE, deliberately. An earlier draft of this migration
-- added a `scan_dispatches` claim table for a services-side weekly scheduler.
-- That was wrong: the proxy ALREADY schedules -- `jobs/search-sweep.ts` polls
-- every SEARCH_SWEEP_INTERVAL and enqueues any seed whose next_scan_after has
-- passed, and its `dueSeeds` already excludes runs in flight. A second
-- scheduler would have been two things deciding when a subject is scanned,
-- which is the duplicate-source-of-truth failure this repo keeps finding.

ALTER TABLE search_seeds
  ADD COLUMN tier_next_scan_after TIMESTAMPTZ;

COMMENT ON COLUMN search_seeds.tier_next_scan_after IS
  'What the ADAPTIVE cadence (search/cadence.py) would have scheduled. '
  'Advisory as of the weekly-cadence change: dispatch is every Sunday '
  'regardless of tier, and '
  'next_scan_after holds what will actually happen. Kept so the cost of '
  'diverging from tiering stays measurable -- compare the two to price the '
  'saving forgone.';

COMMENT ON COLUMN search_seeds.next_scan_after IS
  'When this seed will ACTUALLY next be scanned: the coming Sunday, 00:00 UTC. '
  'Read (via the proxy''s own mirror) by jobs/search-sweep.ts, and published '
  'per INVARIANTS #42 -- so it must never carry a date nothing will act on. '
  'For what the adaptive policy would have chosen, see tier_next_scan_after.';
