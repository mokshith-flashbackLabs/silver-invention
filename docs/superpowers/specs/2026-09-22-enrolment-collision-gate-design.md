# Enrolment collision gate — search may BLOCK, never ASSIGN

**Date:** 2026-09-22
**Status:** approved in conversation by the owner ("owner face or other registered face should not be accepted"); this document is the design to build from.
**Repos:** this one (the gate, the invariant rewording, one migration); `image_backend` (already built — one optional gateway change).

---

## 0. The problem, as observed on production

An on-device household member has no login. Their liveness check runs on the **owner's** phone, started
by the owner's session (the backend authorises this correctly — `SUBJECT_AUTHORITY_SQL`). Whoever is in
front of the camera at that moment is indexed into `identity-v1` under the member's `user_ref`.
Nothing, on either side, asks whether that face already belongs to somebody.

Observed 2026-09-22 00:42 IST: owner Anirudh's session ran on-device member Chai's liveness. In this
instance the frame really was Chai (attribution later resolved two distinct faces, Chai and Anirudh, in
one photo). Nothing enforced it. Had Anirudh scanned his own face:

- two `user_ref`s share one face vector;
- `attribution/resolve.py` picks `max(similarity)` between them — arbitrary at ~99 vs ~99 — so the
  owner's photos become the member's seeds and the owner's **hits render to the member**;
- there is no signal anywhere that this happened.

This is INVARIANTS #1's failure in a new shape: not a search *assigning* an identity, but an enrolment
*duplicating* one. The backend has carried the answer since phase 7 — `409 IDENTITY_CONFLICT`, mapped,
audited, tested against a fake — and it has never fired, because this repo refused to build the half
that raises it. `config.py` says so in as many words: *"THERE IS NO COLLISION CHECK, deliberately.
Wiring this to one means a similarity score influencing enrolment, which is invariant #1."*

That reasoning was right about the danger and wrong about the remedy. The danger in #1 is a score
**choosing** a `user_ref`. A score **refusing** to index a face that already has one cannot mint,
overwrite or merge an identity — the only thing it can produce is a `409` and a session that must be
retaken by the right person. This spec builds exactly that, and words the invariant so it stays true.

---

## 1. The rule

> **A liveness frame that matches an already-enrolled face belonging to a DIFFERENT `user_ref` is not
> indexed. The session is consumed, no enrolment and no `subjects` row are written, and the proxy
> receives `409 identity_conflict`.**

Three properties, each of which a test pins:

1. **The search can only block.** Its result is a `Collision | None`. The module that runs it has no
   store, no `IndexFaces`, no way to write a `user_ref`. `user_ref` still comes from the session row and
   from nowhere else.
2. **Same person re-enrolling is not a collision.** A match whose `ExternalImageId` equals THIS
   session's `user_ref` is ignored. Re-enrolment (a new phone, a fresh scan) must keep working.
3. **Fail closed.** If the search itself is unavailable, nothing is indexed and the proxy gets the same
   `503 face_index_unavailable` it already handles for `IndexFaces` — retry with the same key. An
   unavailable check must not become a skipped check.

---

## 2. Where it sits

`POST /v1/liveness/{sid}/result`, `http/routes/liveness.py`, on the `succeeded` path:

```
provider result succeeded, confidence ≥ threshold
reference + audit frames persisted through the presigned PUTs      (unchanged)
────────────────────────────────────────────────────────────────────
NEW  collision = await collision_check(face_index, cfg, row.user_ref, result.reference_image)
NEW  if collision: finalize_conflict(...) → 409 identity_conflict   (consumed, nothing indexed)
────────────────────────────────────────────────────────────────────
IndexFaces (QualityFilter HIGH) → quality_rejected | enrolled        (unchanged)
```

**Before `IndexFaces`, not after.** After would mean the face is already in the collection when the
conflict is discovered, and undoing that is a `DeleteFaces` racing anything that just searched. Before
means a conflict indexes nothing, which is the same guarantee `quality_rejected` gives.

**Same bytes.** The search uses `result.reference_image`, the bytes `IndexFaces` is about to use —
already in memory from `GetFaceLivenessSessionResults`, no S3 client (CLAUDE.md §3.3), and nothing
else could be searched without breaking the liveness→enrolment binding.

---

## 3. The module — `enrolment/collision.py`

```python
@dataclass(frozen=True)
class Collision:
    matched_user_ref: UserRef
    similarity: float
    model_id: str

class FaceSearcher(Protocol):
    async def search_face(
        self, *, collection_id: str, image_bytes: bytes, threshold: float, max_faces: int
    ) -> tuple[FaceHit, ...]: ...        # raises FaceIndexUnavailable on provider failure

async def collision_check(
    searcher: FaceSearcher, cfg: Config, own_ref: UserRef, image_bytes: bytes
) -> Collision | None:
```

- Calls `SearchFacesByImage(CollectionId=identity-v1, Image={Bytes}, FaceMatchThreshold=
  cfg.enrolment_collision_threshold, MaxFaces=cfg.enrolment_collision_max_faces, QualityFilter=NONE)`.
  `QualityFilter=NONE` on the SEARCH, deliberately: the HIGH filter belongs to `IndexFaces` and a frame
  that would fail it should still be checked, not skipped past.
- Discards every hit whose `ExternalImageId` is `own_ref` or does not parse as a `UserRef` (we set
  every one ourselves — INVARIANTS #6; a non-UUID is something we did not put there).
- Returns the highest-similarity surviving hit as a `Collision`, or `None`.
- `search_face` lives on `RekognitionFaceIndex` beside `index_face`, behind the `FaceSearcher`
  protocol, so the route keeps a single injected dependency and the tests keep a single fake.
- **The module imports no store, no `IndexFaces`, no models that carry a session or an enrolment.**
  `tests/test_boundaries.py` asserts that structurally (§7).

`enrolment_collision_threshold` gets its first reader. `enrolment_collision_max_faces` is new (config,
default 5): the question is "is anyone ELSE above threshold", and one page of matches answers it.

---

## 4. On conflict — what is written

New store method `finalize_conflict(sid, *, confidence, reference_image_uri, audit_image_uris,
collision)`, ONE transaction, mirroring `finalize_quality_rejected`:

1. `liveness_sessions` → `status='consumed'`, `failure_reason='identity_conflict'`, `completed_at`,
   `consumed_at`. **Consumed**: the same bytes would conflict again, and leaving it open would
   `409 PASSED_UNCONSUMED`-lock the person out of the fresh session that IS the remedy. Same reasoning
   the quality-rejected path already carries.
2. `INSERT INTO enrolment_conflicts` (migration **0036**):

   ```sql
   CREATE TABLE enrolment_conflicts (
     conflict_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     session_id         UUID NOT NULL UNIQUE REFERENCES liveness_sessions(session_id),
     attempted_user_ref UUID NOT NULL,
     matched_user_ref   UUID NOT NULL,
     similarity         NUMERIC(5,2) NOT NULL,
     threshold_used     NUMERIC(5,2) NOT NULL,
     model_id           TEXT NOT NULL,
     collection_id      TEXT NOT NULL,
     occurred_at        TIMESTAMPTZ NOT NULL DEFAULT now()
   );
   ```
   Provenance (CLAUDE.md §5): a refused enrolment must be traceable to the search that refused it,
   with the threshold in force at the time — the same reason `attribution_runs` carries
   `match_threshold`. `UNIQUE(session_id)` because a session conflicts at most once. Grants: `SELECT,
   INSERT` to the app role, nothing to `imageshield_proxy_ro` — the matched `user_ref` never crosses.
3. `audit_log` row: `actor_type='system'`, `action='enrolment.identity_conflict'`,
   `subject_ref=attempted_user_ref`, `resource_id=conflict_id`, `metadata={similarity, threshold}`.
   **Not** the matched ref in metadata — it is on the conflicts table for staff with a reason; the
   audit trail is read more widely.
4. **No `enrolments` row, no `subjects` upsert, no `IndexFaces`.** The `svc.v_person_enrolment_state`
   view therefore still reports the person as not enrolled, which is true.

---

## 5. The response

```
409 { "error": { "code": "identity_conflict", "message": "...", "retryable": false,
                 "request_id": "...", "conflict_id": "<uuid>" } }
```

- `conflict_id` rides in the envelope's `**extra`. It is a support handle — an opaque id a human
  can look up on `enrolment_conflicts` — and **nothing about the matched person is on the wire**.
  The backend's own audit comment already says "NEVER the matched person", and this response honours
  it by construction.
- The backend maps `identity_conflict → 409 IDENTITY_CONFLICT` today (`src/enrolment/outcomes.ts`).
  Its gateway reads only `error.code` from a failure body, so `conflict_id` is dropped on the floor
  and its audit row's `conflict_id` is always undefined. **Optional backend half:** lift
  `error.conflict_id` in `gateway.ts` into `details` so that audit row fills. One field, no
  contract change, and the client copy already says "quote the support reference below".
- Idempotent replay: a same-key retry of a conflicted session hits the `completed_at` branch and
  replays `_result_response(row)`, which today assumes pass/fail/quality. Extend it: a row with
  `failure_reason='identity_conflict'` replays the **same 409**, not a `200 passed enrolled:false`.
  A test pins that a replay cannot turn a refusal into a success.

---

## 6. The threshold

`ENROLMENT_COLLISION_THRESHOLD` exists on both sides, unread, at **99** in `.env.example`. Two facts
decide the value:

- Rekognition same-person similarity across two live frames is typically 97–99.9; it dips below 99
  with lighting, angle and the liveness challenge's motion. At 99 the gate would miss the very case
  it exists for often enough to be worthless.
- Different-person similarity above 95 is rare outside identical twins; at 97 a legitimate member
  is refused only when they closely resemble someone already enrolled *in this collection* — which,
  the collection being household-scoped in practice, is a sibling or a parent.

**Recommendation: 97.** A false refusal is a `409` with a support handle and a retake; a false pass is
a permanently duplicated identity. The asymmetry favours the lower number. It is config, so the owner
can move it without a deploy; this spec records 97 as the value to deploy with, subject to the
owner's say. One threshold, one purpose (INVARIANTS #1b) — it is not `face_match_threshold` and not
`attribution_match_threshold`, and the tests assert the gate reads its own key.

---

## 7. The invariant, reworded — and the tests that keep it true

**INVARIANTS #1 gains a paragraph, not a reversal:**

> A face search in the enrolment path may **refuse** an enrolment; it may never **assign, mint, merge
> or overwrite** a `user_ref`. The one such search lives in `enrolment/collision.py`, returns only
> "this face already belongs to a different `user_ref`", and that module cannot write.

`tests/test_boundaries.py`:

- `test_no_face_search_in_the_enrolment_path` — the enrolment-path scan gains **one exemption**,
  `enrolment/collision.py`, named the way `ATTRIBUTION_DIR` is (a constant, a code change to widen).
- **New** `test_the_collision_module_cannot_assign` — `enrolment/collision.py` imports nothing from
  `liveness.store`, `enrolment.store`, `subjects`, and does not reference `IndexFaces`/`index_face`.
  The grep is the check: a module that cannot reach a store cannot assign.
- **New** `test_the_collision_exemption_is_actually_used` — mirrors the attribution one: if
  `collision.py` stops calling face search, the exemption must be deleted, not left standing.
- `test_face_search_appears_only_in_the_attribution_module` — exemption list becomes exactly two
  entries: `attribution/` and `enrolment/collision.py`.

`tests/test_liveness_routes.py`, with the fake gaining `search_face`:

- owner's face already enrolled under `user_ref` A; session for B with the same frame → `409
  identity_conflict`, `conflict_id` present, **no `index_face` call**, no enrolment, no subject,
  session consumed, one `enrolment_conflicts` row, one audit row;
- same frame, same `user_ref` (re-enrolment) → not a conflict, indexes normally;
- a hit **below** threshold → not a conflict;
- a hit whose `ExternalImageId` is not a UUID → discarded, not a conflict;
- `search_face` raises `FaceIndexUnavailable` → `503 face_index_unavailable`, nothing written,
  nothing indexed (fail closed);
- same-key replay of a conflicted session → the same `409`, never a `200`;
- the gate reads `enrolment_collision_threshold`, not `face_match_threshold` (assert the fake saw the
  collision value).

`tests/test_enrolment_store.py` / a new store test: `finalize_conflict` is one transaction — a failure
after the session update leaves the session unconsumed (rollback), never a consumed session with no
conflict row.

---

## 8. What this does NOT do, by decision

- **It does not catch a stranger nobody has enrolled.** If the owner hands the phone to a friend,
  that face is indexed as the member. Closing that needs the member's *photos* as the reference —
  "photos first, then the scan, and the scan must match a face in them" — which reorders the
  on-device flow on every client and adds a second Rekognition operation. It is the right next step
  and it is its own spec; the owner chose to ship this gate first.
- **It does not repair existing duplicates.** Any `user_ref` pair already sharing a face on
  production stays as it is until someone looks; a one-off reconciliation query (search each
  enrolled reference frame against the collection, report pairs) is a `devtools/` task, not part of
  this build.
- **No client change.** The proxy already maps the code; mobile and web already render
  `IDENTITY_CONFLICT` copy from phase 7.
- **`DeleteFaces` is untouched.** A conflict indexes nothing, so there is nothing to delete.

---

## 9. Deploy

Services only: migration 0036, image, `services` (the API) — the worker does not run this path. The
backend needs no deploy unless the optional gateway change ships. Set `ENROLMENT_COLLISION_THRESHOLD`
to the agreed value in the services task definition before the roll; the key is already present and
validated, so a deploy with the old `99` runs the gate at a value that rarely fires — worse than
either choosing 97 or leaving the gate off, because it looks protected.
