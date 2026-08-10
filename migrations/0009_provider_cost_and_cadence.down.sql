-- Reverses 0009.
--
-- `providers.daily_budget_usd` is NOT dropped: it belongs to 0001, which
-- declared it before anything enforced it. Dropping it here would make this
-- migration destructive to a column it never created.
--
-- Reverting removes every spend cap and every breaker, so a provider that was
-- open at the time comes straight back into rotation on the next run. Spend
-- history in `provider_spend` is lost outright -- it is a pre-aggregation of
-- `provider_calls`, which survives, so today's totals are recomputable from
-- there. That recomputation is exactly the SUM() the request path is
-- forbidden from doing; doing it once, offline, after a rollback is fine.

DROP INDEX seeds_due_idx;

ALTER TABLE search_seeds
  DROP COLUMN consecutive_empty_scans,
  DROP COLUMN next_scan_after,
  DROP COLUMN scan_tier;

DROP TABLE provider_spend;

-- Narrowing back to 0001's NUMERIC(10,4). This one IS lossy in principle: a
-- recorded cost with more than 4 decimals is rounded on the way down. In
-- practice nothing can have written such a value unless a provider's
-- cost_per_call_usd was set to sub-cent precision while this migration was
-- applied, and that column is dropped below anyway.
ALTER TABLE provider_calls
  ALTER COLUMN cost_usd TYPE NUMERIC(10,4);

ALTER TABLE provider_calls
  DROP CONSTRAINT provider_calls_status_valid;

ALTER TABLE providers
  DROP COLUMN breaker_cooldown_seconds,
  DROP COLUMN breaker_consecutive_failures,
  DROP COLUMN breaker_reason,
  DROP COLUMN breaker_opened_at,
  DROP COLUMN breaker_state,
  DROP COLUMN rate_limit_per_min,
  DROP COLUMN monthly_budget_usd,
  DROP COLUMN cost_per_call_usd;
