-- 0024 up: the preview endpoint's per-user render ceiling (INVARIANTS #32)
-- counts 'preview.rendered' audit rows in a rolling 24h window; this partial
-- index makes that count one range scan instead of a seq scan over all audit.
CREATE INDEX audit_preview_renders_idx
  ON audit_log (subject_ref, occurred_at)
  WHERE action = 'preview.rendered';
