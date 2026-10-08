-- The provider gate's per-call ESTIMATE for claude_intel (0041: USD 0.45) had fallen below real
-- calls: on dev on 2026-10-08 stage 1's source proposal cost up to 0.71 (265 876 input tokens,
-- 10 searches). Spec 2026-10-08-intel-source-proposal-v2 raises that call to 25 searches and
-- 16 000 output tokens: about 650k input tokens at 2 per MTok (1.30), 16k output at 10 per MTok
-- (0.16) and 25 searches at 10 per thousand (0.25), so 1.71. An estimate below a real call lets a
-- day overshoot its cap by the difference (0041), so the estimate is 2.00.
UPDATE providers SET cost_per_call_usd = 2.000000 WHERE provider_id = 'claude_intel';
