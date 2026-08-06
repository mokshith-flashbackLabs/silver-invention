-- Reverse of 0003_enrolment_requires_consumed_session.up.sql.

ALTER TABLE enrolments DROP CONSTRAINT enrolments_session_consumed_fk;
ALTER TABLE enrolments DROP COLUMN session_status;
ALTER TABLE liveness_sessions DROP CONSTRAINT liveness_sessions_session_id_status_key;
