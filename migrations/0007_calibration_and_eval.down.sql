-- Reverses 0007. Eval data and calibration configs are dropped outright:
-- they are derived artifacts, reproducible by re-running `calibrate observe`
-- against the labelled set, which lives in the eval_items rows themselves.
--
-- Bands computed under a config are lost with the columns. That is correct:
-- without calibration_version a stored band is uninterpretable anyway, and
-- everything reverts to the pre-step-7 state where 'review' is the only
-- value the write path produces.

ALTER TABLE infringements
  DROP CONSTRAINT infringements_band_valid,
  DROP COLUMN band_reason;

ALTER TABLE attestations
  DROP COLUMN calibration_version,
  DROP COLUMN band;

DROP TABLE eval_seed_coverage;
DROP TABLE eval_observations;
DROP TABLE eval_items;
DROP TABLE calibration_configs;
