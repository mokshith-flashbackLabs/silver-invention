-- Reverses 0042 (spec section 3.7, the step-3 down leg). COORDINATED: roll the backend back
-- first -- it reads svc.v_active_scoped_events, optional on its side -- then run this.

-- 1. Guard. The old CHECK cannot hold an event scoped by tags alone, and dropping the column
--    would leave a live event that matches nobody. status alone, not expires_at: nothing
--    writes 'expired', so an event past its expiry still reads 'active', and refusing on it
--    is the conservative choice.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM threat_events
     WHERE cardinality(tags) > 0 AND NOT is_global AND cardinality(domains) = 0
       AND status IN ('active', 'draft')
  ) THEN
    RAISE EXCEPTION '0042 down: an active or draft threat event is scoped by tags alone; retract it first';
  END IF;
END
$$;

-- Dropping the view drops its grant with it.
DROP VIEW IF EXISTS svc.v_active_scoped_events;

REVOKE SELECT, INSERT ON threat_events FROM intel_rw;

-- 2. The widened CHECK, found by its definition like the old one was.
DO $$
DECLARE
  relevance text;
BEGIN
  SELECT c.conname INTO STRICT relevance
    FROM pg_constraint c
   WHERE c.conrelid = 'public.threat_events'::regclass
     AND c.contype = 'c'
     AND regexp_replace(regexp_replace(pg_get_constraintdef(c.oid), ' NOT VALID$', ''),
                        '[[:space:]()]', '', 'g')
         = 'CHECKis_globalORcardinalitydomains>0ORcardinalitytags>0';
  EXECUTE format('ALTER TABLE threat_events DROP CONSTRAINT %I', relevance);
EXCEPTION
  WHEN no_data_found THEN
    RAISE EXCEPTION '0042 down: threat_events has no widened relevance CHECK to drop';
  WHEN too_many_rows THEN
    RAISE EXCEPTION '0042 down: threat_events has more than one widened relevance CHECK';
END
$$;

-- 3. The old CHECK, NOT VALID. A retracted tag-only event keeps its row and loses its tags
--    with the column below; validating would fail on it. New rows are held to it. Unnamed, as
--    0022 wrote it, so a re-up finds it by definition.
--
--    NOT VALID skips the rows that exist. It does NOT skip a later UPDATE of one: Postgres
--    re-checks every CHECK, validated or not, on the new version of any row it updates. Retract
--    never updates such a row, but 0037's down does -- it sets penalty = 0.01 on every row
--    whose penalty is NULL, and every event written since 0037 (all of intel's) has none. Left
--    alone, that UPDATE fails on the row this leg has just grandfathered and takes the whole
--    `down --all` with it. So each such row is given, now, the value 0037's down would give it,
--    while no relevance CHECK exists to refuse the write. The event is retracted and inert, and
--    penalty has been dormant since 0037. This is every row the restored CHECK cannot hold.
UPDATE threat_events SET penalty = 0.01
 WHERE penalty IS NULL AND NOT is_global AND cardinality(domains) = 0;

ALTER TABLE threat_events ADD CHECK (is_global OR cardinality(domains) > 0) NOT VALID;

ALTER TABLE threat_events DROP COLUMN proposal_id, DROP COLUMN tags;
