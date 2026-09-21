-- 0036 — enrolment_conflicts: the provenance row behind 409 identity_conflict.
--
-- The collision gate (enrolment/collision.py, spec 2026-09-22) refuses to
-- index a liveness frame that already belongs to a DIFFERENT user_ref. A
-- refusal must be traceable to the search that produced it — which face it
-- matched, at what similarity, against which threshold and model, in which
-- collection — for the same reason attribution_runs carries match_threshold
-- (CLAUDE.md §5: every derived row carries provenance). This is where that
-- lives. The matched user_ref is HERE and nowhere on the wire: the HTTP
-- response carries conflict_id only.
--
-- UNIQUE(session_id): a session conflicts at most once — the row is written
-- in the same transaction that consumes the session. The FK means a conflict
-- cannot outlive its session.
--
-- Grants follow 0015/0018: privileges attach to the identity_rw module role,
-- login roles are members of it. NO grant to imageshield_proxy_ro — the
-- proxy learns of a conflict through the 409 and its own audit row, never by
-- reading who was matched.

CREATE TABLE enrolment_conflicts (
  conflict_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id         UUID NOT NULL UNIQUE REFERENCES liveness_sessions(session_id),
  attempted_user_ref UUID NOT NULL,
  matched_user_ref   UUID NOT NULL,
  similarity         NUMERIC(5,2) NOT NULL,
  threshold_used     NUMERIC(5,2) NOT NULL,
  model_id           TEXT NOT NULL,
  collection_id      TEXT NOT NULL,
  occurred_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "Who keeps being matched?" is the reconciliation question a reviewer will
-- ask of this table; the FK side is already covered by the UNIQUE.
CREATE INDEX enrolment_conflicts_matched_idx ON enrolment_conflicts (matched_user_ref);

GRANT SELECT, INSERT ON enrolment_conflicts TO identity_rw;
