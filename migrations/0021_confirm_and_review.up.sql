-- Confirm pipeline + review queue.
-- Design: docs/superpowers/specs/2026-08-19-protection-score-design.md §7–§8.
--
-- confirm_state is the machine/human lifecycle of a hit and is DISTINCT from
-- infringements.status (the user's position) and band (calibration). Only a
-- human decision can produce 'confirmed' — the CHECK below is INVARIANTS #19
-- enforced by schema, not by application code.

ALTER TABLE infringements
  ADD COLUMN confirm_state TEXT NOT NULL DEFAULT 'unconfirmed'
    CHECK (confirm_state IN
      ('unconfirmed', 'machine_triaged', 'confirmed', 'rejected', 'duplicate', 'quarantined')),
  ADD COLUMN severity TEXT
    CHECK (severity IN
      ('ncii_suspected', 'explicit_unmatched', 'unassessed', 'benign_copy', 'likely_not_subject')),
  ADD COLUMN confirm_decided_by TEXT,
  ADD COLUMN confirm_decided_at TIMESTAMPTZ,
  ADD COLUMN duplicate_of UUID REFERENCES infringements(infringement_id),
  -- 64-bit perceptual hash (dHash) of the fetched image. A hash, not bytes:
  -- INVARIANTS #9 stands. Signed BIGINT; Python converts with two's complement.
  ADD COLUMN phash BIGINT,
  ADD COLUMN face_match_score NUMERIC(5,2),
  -- Rekognition moderation label names + confidences. Labels are text about
  -- the image, never the image.
  ADD COLUMN moderation_labels JSONB;

ALTER TABLE infringements
  ADD CONSTRAINT infringements_confirmed_needs_human CHECK (
    confirm_state <> 'confirmed'
    OR (confirm_decided_by IS NOT NULL AND confirm_decided_at IS NOT NULL)
  ),
  ADD CONSTRAINT infringements_duplicate_needs_source CHECK (
    (confirm_state = 'duplicate') = (duplicate_of IS NOT NULL)
  );

CREATE INDEX infringements_confirm_state_idx
  ON infringements (user_ref, confirm_state);

-- pHash dedup lookup: "has a human already decided this picture for this user".
CREATE INDEX infringements_decided_phash_idx
  ON infringements (user_ref)
  WHERE phash IS NOT NULL AND confirm_state IN ('confirmed', 'rejected');

CREATE TABLE review_tasks (
  task_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  infringement_id  UUID NOT NULL REFERENCES infringements(infringement_id) ON DELETE CASCADE,
  user_ref         UUID NOT NULL,
  severity         TEXT NOT NULL
                   CHECK (severity IN
                     ('ncii_suspected', 'explicit_unmatched', 'unassessed',
                      'benign_copy', 'likely_not_subject')),
  -- face_match_score, moderation label names, fetched image_url, best-face
  -- bbox, unfetchable detail. Text about the image, never pixels.
  triage           JSONB NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'decided', 'quarantined')),
  decision         TEXT CHECK (decision IN ('confirmed', 'rejected')),
  decided_by       TEXT,
  decided_at       TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- One live task per hit; an 'uncertain' decision keeps the SAME row pending
  -- (the decision history is audit_log's job).
  UNIQUE (infringement_id),
  CONSTRAINT review_tasks_decided_shape CHECK (
    (status = 'decided')
    = (decision IS NOT NULL AND decided_by IS NOT NULL AND decided_at IS NOT NULL)
  )
);

CREATE INDEX review_tasks_queue_idx ON review_tasks (
  (CASE severity
     WHEN 'ncii_suspected'    THEN 0
     WHEN 'explicit_unmatched' THEN 1
     WHEN 'unassessed'        THEN 2
     WHEN 'benign_copy'       THEN 3
     ELSE 4
   END),
  created_at
) WHERE status = 'pending';

-- 0015's rule: a new table joins a role in a migration, in review. The review
-- queue is part of the search/discovery pipeline, so search_rw owns it.
GRANT SELECT, INSERT, UPDATE ON review_tasks TO search_rw;

-- The confirm pass as a provider row so the EXISTING budget/breaker/spend
-- machinery governs it (INVARIANTS #37–#41). kind 'classifier' is the enum
-- value 0001 reserved for exactly this shape. cost_per_call_usd is the
-- WORST-CASE BUNDLE price (1 DetectFaces + up to 3 SearchFacesByImage +
-- 1 DetectModerationLabels at list ~0.001 each) so the one-row budget check
-- stays conservative without modification.
INSERT INTO providers (provider_id, kind, enabled, calibrated, score_version,
                       cost_per_call_usd, score_kind, score_domain)
VALUES ('rekognition_confirm', 'classifier', true, false, 'rekognition-confirm-v1',
        0.005, 'numeric', NULL)
ON CONFLICT (provider_id) DO NOTHING;
