-- 0025 down: return audit_log to write-only for the application.
--
-- Reverting this re-breaks the preview render ceiling and the subject-decisions
-- feed -- that is the correct behaviour for a down migration, not a bug in it.
-- audit_w is created unconditionally by 0015, which runs before this, so the
-- role is guaranteed to exist here and the REVOKE needs no existence guard
-- (0015 grants INSERT on this same table the same way).

REVOKE SELECT ON audit_log FROM audit_w;
