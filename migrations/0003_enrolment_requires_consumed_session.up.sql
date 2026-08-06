-- Step 4 (CLAUDE.md §8): make "no enrolment without a passed liveness session"
-- (INVARIANTS #2) hold at the database level, not just in application code.
--
-- Mechanism: an enrolment row carries session_status, CHECK-pinned to
-- 'consumed', and a composite FK to (session_id, status). Only sessions whose
-- CURRENT status is 'consumed' can be referenced — and only passed sessions
-- are ever consumed (the quality-rejected path consumes without enrolling,
-- which this constraint permits: consumption without enrolment is legal,
-- enrolment without consumption is not). The FK also pins the session row:
-- its status cannot be changed away from 'consumed' while an enrolment
-- references it.
--
-- Ordering consequence for the writing transaction: UPDATE the session to
-- 'consumed' BEFORE inserting the enrolment (the FK is checked per statement).

ALTER TABLE liveness_sessions
  ADD CONSTRAINT liveness_sessions_session_id_status_key UNIQUE (session_id, status);

ALTER TABLE enrolments
  ADD COLUMN session_status liveness_status NOT NULL DEFAULT 'consumed'
    CONSTRAINT enrolments_session_status_consumed CHECK (session_status = 'consumed');

ALTER TABLE enrolments
  ADD CONSTRAINT enrolments_session_consumed_fk
  FOREIGN KEY (session_id, session_status)
  REFERENCES liveness_sessions (session_id, status);
