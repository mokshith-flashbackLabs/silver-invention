-- Down for 0038. The trigger and its function go. The matches the backfill and
-- the trigger wrote STAY: each one is a real person affected by a real
-- "All users" event, and deleting them would take those people's threat
-- charge away rather than undo a schema change.
DROP TRIGGER IF EXISTS subjects_match_global_threats ON subjects;
DROP FUNCTION IF EXISTS match_new_subject_to_global_threats();
