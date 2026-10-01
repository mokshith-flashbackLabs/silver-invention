-- 0045 -- likeness intel: index the two JSON-key lookups behind protection credits
-- (spec docs/superpowers/specs/2026-09-27-likeness-intel-design.md section 4.8; final review
-- M10, 2026-10-01).
--
-- A credit's renewal check is found by intel_runs.request ->> 'event_id' and its renewal
-- proposal by intel_proposals.target ->> 'renews_event_id'. intel/protection_store.py asks
-- both for every listed credit (two LATERAL lookups, newest first) and on every worker tick
-- (the schedule's NOT EXISTS and count(*) per due credit), and both tables grow with every
-- source check. Unindexed, each lookup scanned its table; the backend's admin calls give up
-- after 3 seconds.
--
-- Each index carries the partial predicate every query states (kind = 'renewal_check',
-- kind = 'protection_event') and the keyset order the list reads, so one index serves the
-- LATERAL's ORDER BY ... LIMIT 1 as well as the equality probes. intel_runs_one_open_renewal
-- (0044) covers only queued and running checks, so it cannot serve these.
--
-- Indexes only: no grant changes, no data changes.

CREATE INDEX intel_runs_renewal_event_idx
  ON intel_runs ((request ->> 'event_id'), created_at DESC, run_id DESC)
  WHERE kind = 'renewal_check';

CREATE INDEX intel_proposals_renews_event_idx
  ON intel_proposals ((target ->> 'renews_event_id'), created_at DESC, proposal_id DESC)
  WHERE kind = 'protection_event';
