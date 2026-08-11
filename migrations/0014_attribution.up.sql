-- Attribution: which enrolled person is this face in this photo?
--
-- Screen 16 asks for 50 recently-posted social media photos. Those are the
-- seeds that matter: Hive is image search, so it finds THE IMAGE reposted or
-- altered. The enrolment ReferenceImage is a selfie taken thirty seconds
-- earlier that nobody has ever reposted, so searching it finds nothing --
-- correctly, forever. Attribution is what says which enrolled person a photo
-- should be a seed FOR, and without it monitoring runs perfectly and reports
-- nothing.
--
-- THE FACE IS THE UNIT, NOT THE PHOTO. A photo containing the owner and a
-- stranger is a valid seed for the owner. A household photo containing two
-- enrolled members produces two seeds, one each, independent.

CREATE TABLE attribution_runs (
  run_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  photo_ref         TEXT NOT NULL,      -- the proxy's photo_id, opaque to us
  requested_by      UUID NOT NULL,      -- owner user_ref
  candidate_count   INT NOT NULL,
  -- Recorded per run, not read from config at query time. A later retune
  -- otherwise makes every historical attribution uninterpretable -- the same
  -- reason search_runs.threshold_config exists. model_id is here for
  -- INVARIANTS #4: a score produced by one face model means nothing against
  -- one produced by another.
  match_threshold   NUMERIC(5,2) NOT NULL,
  max_candidates    INT NOT NULL,
  model_id          TEXT NOT NULL,
  faces_detected    INT,
  faces_attributed  INT,
  status            TEXT NOT NULL DEFAULT 'running',
  error_detail      TEXT,
  started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at      TIMESTAMPTZ
);

CREATE INDEX attribution_runs_photo_idx
  ON attribution_runs (photo_ref, started_at DESC);

CREATE TABLE attributed_faces (
  face_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id             UUID NOT NULL REFERENCES attribution_runs(run_id) ON DELETE CASCADE,
  face_index         INT NOT NULL,
  bbox               JSONB NOT NULL,     -- {x,y,w,h} normalised 0-1
  -- DIFFERENT QUANTITIES, deliberately two columns. detect_confidence is
  -- "this region is a face"; match_score is "this face is that person".
  -- Conflating them is how a confident detection of a STRANGER reads as a
  -- confident identification of a user.
  detect_confidence  NUMERIC(5,2) NOT NULL,
  -- NULL means "a face we could not attribute to any candidate". First-class
  -- and expected -- it is most faces in most photos, and it is precisely the
  -- outcome the face-level rule intends. Not an error, not a rejection.
  resolved_user_ref  UUID,
  match_score        NUMERIC(5,2),
  model_id           TEXT NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Mirrors the proxy's own media.photo_faces constraint. A match score with
  -- no person, or a person with no score, is a bug that should fail at the
  -- insert rather than become an unreadable row later.
  CHECK ((resolved_user_ref IS NULL) = (match_score IS NULL))
);

-- One row per detected face per run. A re-run writes a new run.
CREATE UNIQUE INDEX attributed_faces_uniq ON attributed_faces (run_id, face_index);

-- "Which faces resolved to this person" -- the read behind a seed's provenance.
-- Partial: unattributed faces are the bulk of the table and are never the
-- subject of this question.
CREATE INDEX attributed_faces_person_idx ON attributed_faces (resolved_user_ref)
  WHERE resolved_user_ref IS NOT NULL;

-- A seed now records which face produced it. Nullable: seeds predating
-- attribution, and the enrolment ReferenceImage seed, have no attributed face.
ALTER TABLE search_seeds
  ADD COLUMN attributed_face_id UUID REFERENCES attributed_faces(face_id);
