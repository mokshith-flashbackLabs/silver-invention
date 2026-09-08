-- Reverses 0030. Dropping the column loses every resolved preview address,
-- which is derived data: a re-run of the confirm pipeline re-resolves it from
-- the page. Nothing that cannot be recomputed is destroyed here.

ALTER TABLE infringements
  DROP COLUMN IF EXISTS preview_image_url;
