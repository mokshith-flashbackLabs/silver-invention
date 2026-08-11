-- Reverses 0013.
--
-- Dropping last_attempted_at loses only scheduling state -- which rows the
-- recheck loop tried and when. Nothing user-facing depends on it, and
-- `last_checked_at` (the honest one, exposed to the proxy) is untouched.
--
-- The consequence of running this while the recheck worker is deployed is the
-- starvation the column exists to prevent: the due-queue falls back to
-- last_checked_at ordering, and permanently unreachable rows pin the front of
-- every batch. Stop the worker first.

DROP INDEX infringements_recheck_due_idx;

ALTER TABLE infringements DROP COLUMN last_attempted_at;
