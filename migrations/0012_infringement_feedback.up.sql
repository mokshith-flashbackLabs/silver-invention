-- What the user said about a hit.
--
-- The proxy has a report surface with no way to record a reaction, so a user
-- looking at a match of their own face has nowhere to put "that is not me".
--
-- APPEND-ONLY. A user changing their mind writes a SECOND row; the history is
-- the record. Nothing here is ever updated or deleted -- the sequence of what
-- someone said, and when, is the signal a reviewer needs, and an UPDATE would
-- destroy exactly the part that matters.
--
-- THE RULE THAT MATTERS, recorded here because this table is the temptation:
-- `not_me` NEVER adjusts the user's identity vectors, never suppresses future
-- matches from that domain, and never feeds banding. It writes a row for
-- reviewer calibration and does nothing else.
--
-- The reason is specific and not hypothetical. Users reject TRUE positives
-- under distress, and it is common. If rejections retrained the identity index
-- or suppressed a domain, the users most affected by real abuse would
-- systematically degrade their own protection -- and the failure would be
-- invisible, concentrating on precisely the people the product exists for.
--
-- Keep the signal. Do not act on it automatically.

CREATE TABLE infringement_feedback (
  feedback_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  -- ON DELETE CASCADE is safe in the direction that matters: nothing deletes an
  -- infringement (a dead URL is still evidence), so this only fires if a future
  -- hard-delete path is built, and orphaned feedback about a vanished hit would
  -- be uninterpretable anyway.
  infringement_id  UUID NOT NULL REFERENCES infringements(infringement_id)
                     ON DELETE CASCADE,
  -- Denormalised from infringements deliberately: the route checks ownership
  -- before writing, and carrying it here means "what did THIS user say" needs
  -- no join to answer -- including after a schema change upstream.
  user_ref         UUID NOT NULL,
  signal           TEXT NOT NULL CHECK (signal IN ('not_me','confirmed','uncertain')),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "The history of this hit, newest first" is the only read: reviewer
-- calibration, and rendering what the user last said.
CREATE INDEX infringement_feedback_infr_idx
  ON infringement_feedback (infringement_id, created_at DESC);
