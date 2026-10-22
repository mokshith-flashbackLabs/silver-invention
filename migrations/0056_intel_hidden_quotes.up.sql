-- 0056 -- likeness intel: an operator may hide a published quote (2026-10-22, spec
-- docs/superpowers/specs/2026-10-15-intel-lessons-and-evidence-design.md section 13).
--
-- On dev a published quote named a man charged in a criminal case. Nothing in code can tell a
-- private person's name, so the extraction prompt stops quoting such passages (extract-v3) and an
-- operator can hide one already published. A hidden quote stays evidence (corroboration, the
-- lesson figure check and the operator's own reads still see it); it only leaves svc.v_evidence,
-- so no person is shown it, in a new report or, through the backend's serve-time check, in one
-- already sent. Every hide and unhide is audited; the columns hold the current state.

ALTER TABLE intel_excerpts
  ADD COLUMN hidden_at   TIMESTAMPTZ,
  ADD COLUMN hidden_by   TEXT,
  ADD COLUMN hide_reason TEXT,
  ADD CONSTRAINT intel_excerpts_hidden_whole CHECK (
    (hidden_at IS NULL AND hidden_by IS NULL AND hide_reason IS NULL)
    OR (hidden_at IS NOT NULL AND hidden_by IS NOT NULL AND hide_reason IS NOT NULL)
  );

-- The same columns as 0055's, so the backend's contract is unchanged; a hidden quote is left out.
CREATE OR REPLACE VIEW svc.v_evidence AS
SELECT o.owner_kind,
       o.owner_id,
       d.document_id,
       d.publisher_domain,
       d.title                                              AS document_title,
       d.published_at,
       x.excerpt_id,
       x.quote_text                                         AS quote,
       CASE WHEN d.trust = 'listed' THEN d.final_url END    AS link_url,
       o.independent_sources,
       o.approved_at
  FROM intel_published_owners o
  JOIN intel_published_evidence pe ON pe.owner_kind = o.owner_kind AND pe.owner_id = o.owner_id
  JOIN intel_signals s ON s.signal_id = pe.signal_id AND s.status = 'active'
  JOIN intel_excerpts x ON x.excerpt_id = pe.excerpt_id AND x.hidden_at IS NULL
  JOIN intel_documents d ON d.document_id = s.document_id;
