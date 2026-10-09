-- Reverses 0052. A watch already created stays, as an ordinary operator source (its runs, documents
-- and evidence reference it). Roll the worker back first: a worker that still creates watches would
-- fail its insert against the narrowed CHECK on every tick.
DROP INDEX IF EXISTS intel_sources_one_news_watch_per_tag;
ALTER TABLE intel_sources DROP CONSTRAINT IF EXISTS intel_sources_news_watch_shape;
UPDATE intel_sources SET origin = 'operator' WHERE origin = 'news_watch';
ALTER TABLE intel_sources DROP CONSTRAINT intel_sources_origin_valid;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_origin_valid
  CHECK (origin IN ('suggested', 'operator'));
