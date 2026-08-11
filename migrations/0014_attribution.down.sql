-- Reverses 0014.
--
-- Drops the record of which face produced which seed. The SEEDS survive --
-- search_seeds rows are not deleted, they just lose their provenance link --
-- so monitoring continues, but nothing can answer "why is this photo a seed
-- for this person" afterwards. For a system whose output is an accusation
-- about someone's likeness, that provenance is the part worth keeping.
--
-- Order matters: the FK on search_seeds must go before the table it points at.

ALTER TABLE search_seeds DROP COLUMN attributed_face_id;

DROP TABLE attributed_faces;

DROP TABLE attribution_runs;
