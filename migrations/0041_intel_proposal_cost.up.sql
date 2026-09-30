-- Likeness intel step 2 (spec section 3.9, amended 2026-09-30): the proposal step calls
-- INTEL_PROPOSAL_MODEL (claude-opus-5-5). The step-0 findings priced its worst-case call at
-- USD 0.45 (60k input tokens at 4 per MTok, 8k output tokens at 20 per MTok, plus five web
-- searches). 0039 shipped 0.25, the Sonnet-only figure, because step 1 never called Opus.
--
-- The provider gate checks this ESTIMATE before a call and records the ACTUAL cost after, so
-- an estimate below a real call's cost lets a day overshoot the cap by the difference. The
-- estimate is the worst case of any model the worker calls. Money in providers is an
-- unquoted numeric literal (the 0009 and 0029 form).
UPDATE providers SET cost_per_call_usd = 0.450000 WHERE provider_id = 'claude_intel';
