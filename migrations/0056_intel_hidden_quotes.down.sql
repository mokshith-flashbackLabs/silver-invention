-- Reverses 0056: every hidden quote is published again (the view as 0055 made it), and the hide
-- state goes. The audit rows of each hide and unhide stay.
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
  JOIN intel_excerpts x ON x.excerpt_id = pe.excerpt_id
  JOIN intel_documents d ON d.document_id = s.document_id;

ALTER TABLE intel_excerpts
  DROP CONSTRAINT intel_excerpts_hidden_whole,
  DROP COLUMN hide_reason,
  DROP COLUMN hidden_by,
  DROP COLUMN hidden_at;
