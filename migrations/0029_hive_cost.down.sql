-- Back to "we cannot price a Hive call".
--
-- NULL rather than a remembered previous value: before 0029 the column had
-- never held one, and the down of a migration whose whole content is a
-- measurement should restore the ABSENCE of the measurement. A daily budget
-- set while 0029 was applied then correctly starts failing closed again —
-- refusing to dispatch against a cap it cannot enforce — which is the
-- behaviour INVARIANTS #38 asks for and not a regression.
UPDATE providers SET cost_per_call_usd = NULL WHERE provider_id = 'hive';
