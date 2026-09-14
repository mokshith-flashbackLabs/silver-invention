-- 0033 down. The verdicts are a measurement, not product state: dropping the
-- table loses the labels, which is what a down migration of a new table means
-- here. Nothing else reads them.
DROP INDEX audit_operator_preview_renders_idx;
DROP INDEX infringements_feed_keyset_idx;
DROP TABLE review_verdicts;
