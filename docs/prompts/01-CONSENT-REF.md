# Out-of-band task — `enrolments.consent_ref`

**Not a numbered step.** Small, self-contained, and unblocking the proxy repo's phase 6. Run it on
`main` after step 7 merges, or on its own branch off `main` — do not fold it into the step-7 branch.

---

## Why this exists

`INVARIANTS.md` #2 says: *no enrolment without a passed liveness session **and a signed consent
record***. The first half is enforced by the `UNIQUE` FK on `enrolments.session_id`. The second half
is not enforced at all — there is no consent table in this repo, so **nothing currently prevents a
consent-free enrolment.** For Article 9 biometric processing that is the wrong state to be in.

The original design put `consent_records` in this repo. That was wrong, and it is being reversed:

**The proxy owns consent.** It has `profile.persons`, `profile.guardianships` with `subject_dob`
triggers, and `profile.v_consent_eligibility` computing `required_signer_role` and `blocked_reason` —
the hard part, already built. It is also the only public ingress, so the DocuSeal webhook must
terminate there. This repo knows a `user_ref` and a face vector; it cannot determine who is required
to sign.

**This repo holds a reference, not a record.** Enforcement survives. The document does not cross the
boundary.

---

## Migration 0010

```sql
-- up
ALTER TABLE enrolments
  ADD COLUMN consent_ref             UUID,
  ADD COLUMN consent_document_sha256 TEXT,
  ADD COLUMN consent_signed_at       TIMESTAMPTZ;

-- Dev/test rows created before consent was required. Backfill with a sentinel
-- so NOT NULL can be applied. This UUID is reserved and must never be issued
-- by the proxy.
UPDATE enrolments
   SET consent_ref             = '00000000-0000-0000-0000-000000000000',
       consent_document_sha256 = 'PRE_CONSENT_TEST_DATA',
       consent_signed_at       = created_at
 WHERE consent_ref IS NULL;

ALTER TABLE enrolments
  ALTER COLUMN consent_ref             SET NOT NULL,
  ALTER COLUMN consent_document_sha256 SET NOT NULL,
  ALTER COLUMN consent_signed_at       SET NOT NULL;

-- The sentinel is a migration artifact, not a valid state going forward.
ALTER TABLE enrolments
  ADD CONSTRAINT enrolments_consent_not_sentinel CHECK (
    consent_ref <> '00000000-0000-0000-0000-000000000000'
      OR created_at < '2026-08-11'::timestamptz
  );

CREATE INDEX enrolments_consent_idx ON enrolments (consent_ref);
```

Set the CHECK date to the migration date. It permits existing dev rows and forbids new ones — a fresh
enrolment carrying the sentinel fails at the database, not in application code.

If the table is empty, drop the UPDATE and the CHECK entirely and add the columns `NOT NULL`
directly. Report which path you took.

Number the migration to follow whatever is currently highest in `migrations/` — the number above
assumes 0009 is the last one.

---

## API change

`POST /v1/liveness/{session_id}/result` gains three required body fields:

```
{
  reference_put_url, audit_put_urls[],          # existing
  subject_is_adult,                             # existing (step 8)
  consent_ref:             uuid,                # NEW, required
  consent_document_sha256: str,                 # NEW, required
  consent_signed_at:       iso8601              # NEW, required
}
```

- Any absent → `400` with code `consent_required`. Do not default, do not infer.
- `consent_ref` must be a well-formed UUID and must not be the sentinel → `422` otherwise.
- `consent_signed_at` in the future → `422`.
- Persist all three onto the `enrolments` row inside the **same transaction** as `IndexFaces`
  succeeding and the session being consumed.

The proxy is responsible for having actually collected consent. We record the evidence and make its
absence structurally impossible. We do not verify the signature — we cannot, we never see the
document.

`GET /v1/liveness/{session_id}` gains `consent_ref` in its response so the proxy can reconcile.

**No new endpoint.** `POST /v1/users/{id}/consent` is being removed from the contract, not built.

---

## Doc updates in the same commit

- `INVARIANTS.md` #2 — reword to *"a passed liveness session and a **consent reference supplied by
  the proxy**"*, and note that the document itself lives in the proxy.
- `PROXY_INTEGRATION.md` §4 — delete the `POST /v1/users/{id}/consent` row. Add the three new fields
  to the liveness result row.
- `PROXY_INTEGRATION.md` §5 — **delete this section entirely.** It says services mint the presigned
  PUT and own the bucket, which contradicts §1 of the same file ("Proxy owns... minting presigned S3
  URLs") and `CLAUDE.md` §3.3 ("We hold no AWS S3 credentials"). §1 and CLAUDE.md are correct. The
  5-minute TTL in §5 also contradicts the ≥15-minute floor the step prompts require.
- `SCHEMA.md` — the `consent_records` table is no longer ours. Replace it with a note that consent
  lives in the proxy and we hold `consent_ref`.
- `CLAUDE.md` §3 — move consent from "What this repo owns" to "What the proxy owns".

---

## Done when

- migration runs clean forward and backward
- an enrolment attempt with no `consent_ref` returns `400 consent_required` and writes **no** row
- an enrolment attempt with the sentinel UUID is rejected by the database CHECK, not by application
  code — assert by attempting the insert directly in SQL
- a future-dated `consent_signed_at` returns `422`
- all three fields land on the `enrolments` row in the same transaction as the index and the session
  consume — assert by killing the process mid-write and confirming no partial row
- `GET /v1/liveness/{sid}` returns `consent_ref`
- `grep -rn "consent" src/` shows no DocuSeal client, no document rendering, no hashing of a document
  — we store a hash the proxy computed, we never compute one
- the five doc updates are in the same commit as the code

Stop when done. This is out-of-band; return to the numbered steps afterwards.

---

## Standing rules

```
- Cite file:line when describing existing behaviour. Mark anything not read
  directly as INFERRED.
- Never port logic from the old server.js. Read it to learn what a feature did,
  never how it did it.
- Every SQS consumer is idempotent. Delivery is at-least-once and the outbox
  makes duplicates normal, not exceptional.
- Messages carry IDs, never payloads. Workers re-read from Postgres; the stored
  row wins.
- If anything here conflicts with CLAUDE.md §4 (invariants), STOP AND ASK. Do not
  resolve it yourself. That rule has already caught four real contradictions in
  these documents.
- Doc corrections land in the same commit as the code they describe.
- When the task is done, STOP. Report before starting the next one.
```
