-- Step 3 (liveness lifecycle): the result endpoint requires an
-- Idempotency-Key (CLAUDE.md §9). The stored key is what distinguishes an
-- idempotent retry of the SAME result call (replay the stored outcome, 200)
-- from a genuine second use of a single-use session (410).
--
-- Plain TEXT, not an FK or a lookup table: one session has exactly one
-- result, so the key lives on the row it protects.

ALTER TABLE liveness_sessions ADD COLUMN result_idempotency_key TEXT;
