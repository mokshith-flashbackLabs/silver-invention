-- Reverses 0012.
--
-- Drops every recorded user reaction. That is a genuine loss: the feedback is
-- append-only precisely because the sequence of what someone said is the
-- record, and there is no other copy of it -- the proxy stores none of this.
-- Reversibility is a CI requirement (build spec Phase 1 §5) so the pair exists.
--
-- `infringements.status` values written from feedback ('dismissed_not_me',
-- 'acknowledged') SURVIVE this, and deliberately: they are the user's current
-- position on a hit, and resetting them would re-surface a match someone has
-- already dismissed. The evidence of *why* the status was set is what is lost.
--
-- Recorded here rather than in a runbook because this file is what an operator
-- reads immediately before running it.

DROP TABLE infringement_feedback;
