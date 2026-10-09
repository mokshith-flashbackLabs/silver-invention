-- The daily news watch (spec 2026-10-09-intel-news-watch-design §2.3): code creates one saved search
-- per mapped platform tag, so origin gains 'news_watch', and a partial unique index keeps it to ONE
-- watch per tag, which is also what makes the worker's create idempotent (ON CONFLICT DO NOTHING).
ALTER TABLE intel_sources DROP CONSTRAINT intel_sources_origin_valid;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_origin_valid
  CHECK (origin IN ('suggested', 'operator', 'news_watch'));
-- A watch is a saved search naming exactly one tag.
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_news_watch_shape
  CHECK (origin <> 'news_watch' OR (kind = 'search_query' AND cardinality(tags) = 1));
CREATE UNIQUE INDEX intel_sources_one_news_watch_per_tag
  ON intel_sources ((tags[1])) WHERE origin = 'news_watch';
