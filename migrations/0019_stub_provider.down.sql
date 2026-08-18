-- Reverses 0019. Deletes ONLY the row this migration inserted — 0004's hive
-- and google rows are untouched.
DELETE FROM providers WHERE provider_id = 'stub';
