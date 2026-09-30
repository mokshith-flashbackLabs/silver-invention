-- Reverses 0039: claude_intel goes back to no budget, which refuses every run
-- (budget_unset) until a budget is set again.
UPDATE providers SET daily_budget_usd = NULL WHERE provider_id = 'claude_intel';
