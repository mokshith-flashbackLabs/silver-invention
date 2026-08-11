# `service-role.json`

The IAM policy for every ImageShield process. Kept as a standalone file, not
inline in `iam.tf`, so that **Terraform and the test suite read the same
artifact**: `infra/terraform/iam.tf` renders it with `templatefile()`, and
`tests/test_iam_policy.py` parses it directly. A policy asserted from a copy is
asserted about nothing.

The file contains no comments, and that is not an oversight — an IAM policy
document accepts only `Version`, `Id` and `Statement`, and AWS rejects the
whole document if anything else appears at the top level. The explanation lives
here instead, and a test asserts the top-level keys stay legal.

## What is absent is the point

**No `s3:` action of any kind, not even `GetObject`.**

`CLAUDE.md` §3.3: this service holds no S3 credentials. The presigned-URL
handshake exists precisely so it never needs them — the proxy mints PUT/GET
URLs, we read or write through them and discard the bytes. Grant the role one
S3 permission and that handshake becomes optional, which means one day it gets
skipped, and the boundary that keeps image bytes out of this service becomes a
convention again.

With no grant, a future "let's just read it from S3" **cannot work even if
somebody writes the code**. The mistake is impossible rather than forbidden.

This is one of only three places in the system where the data boundary is
enforced by something other than discipline. The other two are the schema lint
(step 2, no `bytea` column anywhere) and the structlog redaction processor
(step 1, no phone-shaped string in a log line).

## Scoping notes

- **Collection operations are scoped to the collection ARN.** `IndexFaces` into
  somebody else's collection is not a call this service should be able to make.
  This includes `SearchFacesByImage`: attribution's one permitted face search
  (INVARIANTS #1a) cannot reach another collection even if the narrowed grep
  gate were removed.
- **`DetectFaces` cannot be collection-scoped** — it takes no collection, so
  there is no resource to name. It is a pure image operation and grants no
  access to enrolled identities.
- **Liveness session APIs cannot be scoped either.** The session does not exist
  when `CreateFaceLivenessSession` is called, so there is no ARN to name.
- **Secrets are scoped to our prefix**, `imageshield/<environment>/*`, not `*`.
  `GetSecretValue` on `*` reads every secret in the account, including ones
  belonging to systems that have nothing to do with this one.

## Adding a permission

Add it to the JSON, and add a case to `tests/test_iam_policy.py` naming the
call in `src/` that needs it. The test that asserts *every granted action is
used* does not exist, but the one asserting *every used action is granted*
does — so a grant added without a caller is a grant nobody can justify at
review.
