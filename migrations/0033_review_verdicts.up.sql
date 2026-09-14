-- 0033 up: reviewer VERDICTS — a measured false-positive rate for the face
-- matcher, and the indexes the reviewer hit feed needs.
--
-- WHY THIS TABLE IS NOT A COLUMN ON `infringements`, AND NOT `review_tasks`.
-- Face matching runs on Rekognition today and the team is replacing it with
-- its own models. Nobody can compare the two without a human looking at real
-- hits and recording whether the MACHINE was right — which is a different
-- question from whether the hit is infringing. Owner decision D6 (2026-09-14):
-- a verdict is a LABEL. It never changes `confirm_state`, never touches
-- `review_tasks`, and `review/store.py::decide` remains the only override.
-- Two reviewers may label the same hit and both rows survive; the feed reads
-- the latest.
--
-- The three snapshot columns (`machine_severity`, `face_match_score`,
-- `confirm_state_at_verdict`) are copied AT THE MOMENT OF THE VERDICT rather
-- than joined at read time. A false-positive rate computed against a severity
-- an operator later overrode would measure the override, not the machine.

CREATE TABLE review_verdicts (
  verdict_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  infringement_id          UUID NOT NULL
                           REFERENCES infringements(infringement_id) ON DELETE CASCADE,
  operator                 TEXT NOT NULL CHECK (operator <> ''),
  verdict                  TEXT NOT NULL
                           CHECK (verdict IN ('true_positive', 'false_positive', 'unsure')),
  -- Text about the review, never about the image (INVARIANTS #9).
  note                     TEXT,
  machine_severity         TEXT
                           CHECK (machine_severity IN
                             ('ncii_suspected', 'explicit_unmatched', 'unassessed',
                              'benign_copy', 'likely_not_subject')),
  face_match_score         NUMERIC(5,2),
  confirm_state_at_verdict TEXT NOT NULL
                           CHECK (confirm_state_at_verdict IN
                             ('unconfirmed', 'machine_triaged', 'confirmed',
                              'rejected', 'duplicate', 'quarantined')),
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "the latest verdict per hit" — the feed's DISTINCT ON.
CREATE INDEX review_verdicts_latest_idx
  ON review_verdicts (infringement_id, created_at DESC);

-- the stats window: every verdict since a timestamp.
CREATE INDEX review_verdicts_window_idx
  ON review_verdicts (created_at DESC);

-- APPEND-ONLY, the score_events shape (0022): SELECT + INSERT, no UPDATE and
-- no DELETE, for the same reason — an editable measurement is not a
-- measurement. The review queue belongs to search_rw (0021's ruling), so the
-- verdicts on it do too.
GRANT SELECT, INSERT ON review_verdicts TO search_rw;

-- The feed's keyset cursor orders by (first_seen_at DESC, infringement_id
-- DESC); without this index every page is a sort of the whole table.
CREATE INDEX infringements_feed_keyset_idx
  ON infringements (first_seen_at DESC, infringement_id DESC);

-- The per-OPERATOR render ceiling (INVARIANTS #32, now two ceilings). 0024's
-- index is keyed on subject_ref, which an operator render does not filter on
-- — the operator's name lives in the metadata. Same partial-index shape,
-- keyed on that expression instead, and additionally filtered to
-- actor_type = 'operator' so the subject's own rows never enter it.
CREATE INDEX audit_operator_preview_renders_idx
  ON audit_log ((metadata ->> 'operator'), occurred_at)
  WHERE action = 'preview.rendered' AND actor_type = 'operator';
