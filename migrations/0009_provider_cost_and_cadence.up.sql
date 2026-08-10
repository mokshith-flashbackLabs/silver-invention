-- Step 8, part two: cost tracking, circuit breakers, kill switches, cadence.
--
-- Provider cost is the first thing that binds as this system scales, and it
-- binds before any technical limit does:
--
--   users x seeds x providers x cadence = calls
--   1M users x 3 seeds x 2 providers x weekly = 6M calls/week
--
-- That grows linearly with no natural ceiling. Nothing here is optimisation;
-- it is the difference between a cost line you control and one you discover
-- on an invoice.
--
-- `providers.daily_budget_usd` already exists (0001:61) and is deliberately
-- NOT re-added below -- it was declared before it was enforced, and this
-- migration is what starts enforcing it.
--
-- No GRANTs, matching 0004-0008: per-module DB roles are step 9.

ALTER TABLE providers
  ADD COLUMN cost_per_call_usd  NUMERIC(10,6),
  ADD COLUMN monthly_budget_usd NUMERIC(10,2),
  ADD COLUMN rate_limit_per_min INT,
  ADD COLUMN breaker_state      TEXT NOT NULL DEFAULT 'closed'
                                CHECK (breaker_state IN ('closed','open','half_open')),
  ADD COLUMN breaker_opened_at  TIMESTAMPTZ,
  ADD COLUMN breaker_reason     TEXT,
  -- Two columns the three-state machine needs that the step-8 brief's DDL
  -- sketch left implicit:
  --
  --   breaker_consecutive_failures -- "count consecutive failures" has to
  --     count somewhere durable. In-process state would reset on every deploy
  --     and would be per-worker, so N workers would each need N failures to
  --     open one provider's breaker.
  --
  --   breaker_cooldown_seconds -- "failure re-opens with a doubled cooldown"
  --     needs the CURRENT cooldown to double. Deriving it from
  --     breaker_opened_at cannot work: that timestamp says when we opened,
  --     not how long we had decided to wait.
  ADD COLUMN breaker_consecutive_failures INT NOT NULL DEFAULT 0,
  ADD COLUMN breaker_cooldown_seconds INT;

-- The status vocabulary was a comment on 0001:103 and is now load-bearing:
-- `providers_succeeded` is derived from it, and a typo'd status silently
-- becomes a not-ok row, which reads downstream as a provider outage. Three
-- values are new in this step -- a provider skipped by the budget guard, by
-- an open breaker, or by the kill switch. All three are recorded, none of
-- them is a failure, and none of them fails the run.
ALTER TABLE provider_calls
  ADD CONSTRAINT provider_calls_status_valid CHECK (
    status IN ('ok','error','rate_limited','timeout',
               'budget_exceeded','breaker_open','provider_disabled')
  );

-- Same scale problem, same fix: 0001 declared this NUMERIC(10,4), which cannot
-- represent a 6-decimal per-call price. A widening is non-destructive and every
-- existing value round-trips exactly. Without it the per-call audit trail
-- disagrees with the pre-aggregated total it is supposed to explain.
ALTER TABLE provider_calls
  ALTER COLUMN cost_usd TYPE NUMERIC(12,6);

-- Pre-aggregated, one row per provider per day. The budget guard reads this
-- on the dispatch path and must never SUM(provider_calls): that table grows
-- with every call ever made, so the check that exists to protect spend would
-- itself get slower in proportion to how much has been spent.
-- cost_usd is NUMERIC(14,6), NOT (12,4). The scale must be at least the scale of
-- what accumulates into it — cost_per_call_usd above is NUMERIC(10,6) — because
-- the upsert coerces each increment to the column type BEFORE adding, so a
-- narrower accumulator rounds every single call rather than rounding the total
-- once. Measured against Postgres 16 with this exact DDL and the real upsert:
--
--   price      10 calls at (12,4)   true value    error
--   0.001250   0.0130               0.012500      +4%     cap binds early
--   0.000250   0.0030               0.002500      +20%    cap binds early
--   0.000040   0.0000               0.000400      total   NEVER binds
--
-- The last row is the one that matters: below 0.00005/call the accumulator never
-- grows, the guard reads spent=0 forever, and a configured daily budget silently
-- stops binding. That is a fail-OPEN in the one check INVARIANTS #38 requires to
-- fail closed. Google's 0.003500 is exact at 4 dp, so nothing would have shown
-- today — it would have surfaced on the first contract price quoted per thousand
-- calls with an odd third cent digit.
--
-- 14 digits of precision holds ~99,999,999.999999 USD of daily spend per
-- provider, which is several orders of magnitude past anything real.
CREATE TABLE provider_spend (
  provider_id   TEXT NOT NULL REFERENCES providers(provider_id),
  spend_date    DATE NOT NULL,
  call_count    INT NOT NULL DEFAULT 0,
  cost_usd      NUMERIC(14,6) NOT NULL DEFAULT 0,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (provider_id, spend_date)
);

-- Adaptive cadence: the 4-10x lever, and the only one here that bends the
-- curve rather than capping it. A user with no hits in six months does not
-- need weekly scans.
--
-- 'standard' as the default is the safe direction: a seed whose tier was
-- never computed is scanned weekly, not monthly.
ALTER TABLE search_seeds
  ADD COLUMN scan_tier TEXT NOT NULL DEFAULT 'standard'
             CHECK (scan_tier IN ('new','standard','relaxed','dormant','priority')),
  ADD COLUMN next_scan_after TIMESTAMPTZ,
  ADD COLUMN consecutive_empty_scans INT NOT NULL DEFAULT 0;

-- The scheduler that reads this is the recheck loop, which is specified and
-- out of scope (CLAUDE.md §6). The index exists with the column because
-- "which active seeds are due" is the only query the column has, and adding
-- it later means an index build on a table that by then holds every seed.
CREATE INDEX seeds_due_idx ON search_seeds (next_scan_after)
  WHERE status = 'active';

-- ── Real cost figures ─────────────────────────────────────────────────────
--
-- google: Cloud Vision Web Detection is published list price -- USD 3.50 per
-- 1000 units for the first 1M units/month, i.e. 0.003500 per call. One
-- annotate request with one WEB_DETECTION feature is one unit
-- (src/imageshield/search/google.py sends exactly that).
--
-- hive: DELIBERATELY NULL. Hive Web Search is contract-priced and no measured
-- or quoted per-call figure exists anywhere in this repo -- the devtools
-- harness measured the LIVENESS cost (~USD 0.015/check, now
-- LIVENESS_COST_PER_CHECK_USD) and nothing else. Writing a plausible number
-- here would be worse than leaving it absent: the budget guard would then
-- enforce a cap computed from a figure nobody sourced, and the error would
-- only surface on an invoice.
--
-- Both budgets are left NULL, which the guard reads as "no cap configured"
-- and dispatches freely -- unchanged from the pre-step-8 behaviour. The
-- misconfiguration the guard refuses to paper over is a budget set WITHOUT a
-- cost: that provider is skipped with status='budget_exceeded' rather than
-- dispatched, because an operator who asked for a spend cap must not get
-- unbounded spend just because we cannot price the calls.
--
-- FOLLOW-UP: fill hive.cost_per_call_usd and both daily_budget_usd values
-- from the signed Hive agreement. Until then the mechanism is built, tested,
-- and enforcing nothing for hive.
UPDATE providers SET cost_per_call_usd = 0.003500 WHERE provider_id = 'google';
