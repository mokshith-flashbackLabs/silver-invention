-- One column the recheck loop needs, and the reason it is not last_checked_at.
--
-- `last_checked_at` means "the last time we LEARNED something about whether
-- this URL is still online". Only a definite verdict writes it -- a 2xx/3xx,
-- or a 404/410. It is exposed on GET /v1/search/infringements, and the proxy
-- uses it to decide whether it may tell a user "this came down". A probe that
-- timed out, hit a 5xx, or was refused by the egress guards learned NOTHING,
-- and stamping it would make the row read as freshly verified when nobody
-- successfully looked.
--
-- But the due-queue has to be ordered by something, and ordering by
-- last_checked_at alone starves: a host that is permanently unreachable keeps
-- its NULL, stays at the front of every batch forever, and once there are more
-- such rows than RECHECK_BATCH_SIZE nothing else is ever checked again. The
-- loop would look healthy and silently stop working.
--
-- So: two timestamps with two different meanings. `last_attempted_at` moves on
-- EVERY probe including the ones that failed, and is what the queue orders by.
-- A just-failed row sorts to the back and is retried next cycle without
-- blocking anyone.
--
-- NOT in the task brief. Added because following the brief exactly produces a
-- loop that stops draining, and the alternative fix -- stamping last_checked_at
-- on failures -- would break the honesty property the brief is explicitly
-- about.

ALTER TABLE infringements ADD COLUMN last_attempted_at TIMESTAMPTZ;

-- The due-queue's covering index: live rows, ordered by when we last tried.
-- Partial on url_alive because a dead URL is never re-probed -- resurrection
-- is real but rare, the row already told the user the truth at the time, and
-- re-probing every dead URL forever is not worth catching it.
CREATE INDEX infringements_recheck_due_idx
  ON infringements (last_attempted_at NULLS FIRST)
  WHERE url_alive = true;
