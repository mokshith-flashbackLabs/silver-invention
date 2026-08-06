"""Liveness session lifecycle (CLAUDE.md §8 step 3).

Three I/O ports, each behind a Protocol so the routes are testable without
AWS, S3, or Postgres:

- :mod:`imageshield.liveness.store` — the ``liveness_sessions`` table.
- :mod:`imageshield.liveness.provider` — Rekognition Face Liveness.
- :mod:`imageshield.liveness.uploader` — presigned PUTs into the proxy's S3.

Identity never comes from a similarity score: ``user_ref`` arrives in the
request and there is no face-search call anywhere in this package
(INVARIANTS.md #1). Enrolment/indexing is step 4 and does not live here.
"""
