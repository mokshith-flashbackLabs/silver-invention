-- Separate the durable identifier from the expiring credential.
--
-- THE BUG: search_seeds.source_object_uri stored a presigned GET URL. The
-- intent was right -- no image bytes transit this service -- but a presigned
-- URL is a short-lived CREDENTIAL, not a durable identifier. SigV4 caps at
-- 7 days. So:
--
--   Week 1   seed created, URL valid, the provider fetches, everything works
--   Week 2+  expired; S3 returns 403; the provider cannot fetch. Permanently.
--
-- It does not appear in testing, because fresh seeds work. It surfaces on the
-- second scheduled scan of any seed, a week later, and presents as "Hive is
-- failing" rather than "our URLs expired" -- step 8's zero-successful-calls
-- alarm would fire and point at the wrong thing.
--
-- THE FIX: the expiring thing lives on the expiring object. The seed keeps an
-- opaque durable reference; each RUN carries a freshly-minted presigned GET,
-- supplied by the proxy at enqueue. S3 credentials stay entirely on the proxy
-- and no services-to-proxy call is introduced.
--
-- Note this is search_seeds only. enrolments.source_object_uri is a different
-- column -- the ReferenceImage pointer, named in INVARIANTS #9 -- and does not
-- move.

ALTER TABLE search_seeds RENAME COLUMN source_object_uri TO source_object_ref;

COMMENT ON COLUMN search_seeds.source_object_ref IS
  'Opaque durable reference to the proxy''s S3 object. NEVER a presigned URL — '
  'those expire and this column does not. The proxy resolves it and mints a '
  'fresh presigned GET per search run.';

ALTER TABLE search_runs ADD COLUMN seed_url TEXT;

-- Existing rows hold expired or soon-to-expire presigned URLs on their seed.
-- They are dev data and CANNOT be repaired: a presigned URL cannot be turned
-- back into an object key. '' rather than a fabricated value, so a run that
-- somehow reaches dispatch fails visibly instead of looking valid. The seeds
-- themselves keep their dead URLs verbatim under the new name -- the count of
-- those is reported, and the proxy re-registers them. Nothing is salvaged and
-- nothing is silently rewritten.
UPDATE search_runs SET seed_url = '' WHERE seed_url IS NULL;

ALTER TABLE search_runs ALTER COLUMN seed_url SET NOT NULL;
