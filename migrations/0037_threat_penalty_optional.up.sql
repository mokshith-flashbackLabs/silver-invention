-- 0037 — threat events stop carrying a penalty (spec 2026-09-24,
-- remove-protection-score).
--
-- `penalty` fed exactly one thing: the `threat` component of the services
-- PROTECTION score, which is being deleted. The user-facing score was never
-- fed by it (the backend charges by severity since its 0049). So a new event
-- stores NULL. The column stays for one release, with the dormant score tables
-- (0022), so this is reversible; a later migration drops all of them together.
--
-- 0022 declared `penalty NUMERIC(5,2) NOT NULL CHECK (penalty > 0)` with an
-- unnamed CHECK, which Postgres named threat_events_penalty_check.
-- `penalty_applied` on the matches had no CHECK.

ALTER TABLE threat_events ALTER COLUMN penalty DROP NOT NULL;
ALTER TABLE threat_events DROP CONSTRAINT threat_events_penalty_check;
ALTER TABLE threat_events
  ADD CONSTRAINT threat_events_penalty_check CHECK (penalty IS NULL OR penalty > 0);
ALTER TABLE threat_event_matches ALTER COLUMN penalty_applied DROP NOT NULL;
