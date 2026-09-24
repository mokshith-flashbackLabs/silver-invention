-- Reverse 0037. Rows written after 0037 carry NULL; give them the smallest
-- value 0022's CHECK allows before restoring NOT NULL, or the ALTER fails.
UPDATE threat_event_matches SET penalty_applied = 0.01 WHERE penalty_applied IS NULL;
ALTER TABLE threat_event_matches ALTER COLUMN penalty_applied SET NOT NULL;
UPDATE threat_events SET penalty = 0.01 WHERE penalty IS NULL;
ALTER TABLE threat_events DROP CONSTRAINT threat_events_penalty_check;
ALTER TABLE threat_events ADD CONSTRAINT threat_events_penalty_check CHECK (penalty > 0);
ALTER TABLE threat_events ALTER COLUMN penalty SET NOT NULL;
