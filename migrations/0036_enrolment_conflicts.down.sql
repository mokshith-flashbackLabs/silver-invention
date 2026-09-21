-- Reverse 0036. Revoke before dropping (0035's rule #1: a role holding
-- privileges cannot be dropped, and the error names the role, not the grant).
-- The role itself is 0015's and stays.
REVOKE ALL ON enrolment_conflicts FROM identity_rw;
DROP TABLE enrolment_conflicts;
