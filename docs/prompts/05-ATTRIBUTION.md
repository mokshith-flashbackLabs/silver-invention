# Task 05 — Attribution: face-level seed registration

Run after 01–04. This is the narrow attribution module — **not** the full shield rule.

---

## The rule

For each face detected in an uploaded photo:

```
matches an enrolled person  -> register a seed for THAT person
matches nobody              -> ignore. Not an error, not a rejection.
two enrolled people         -> two seeds, one each, independent
```

**The face is the unit, not the photo.** A photo containing the owner and a stranger is a valid
seed for the owner. A household photo containing two enrolled members produces two seeds.

This replaces the proxy's current gate, which rejects the whole photo when any face is unattributed
(`image_backend/src/media/ports.ts`, `registerSeed`). That gate throws away a valid seed for a
person because somebody else was in frame. The proxy will be changed to match; the reasoning is at
the end of this document.

---

## Why this is needed at all

Screen 16 of the mobile onboarding asks for 50 recently-posted social media photos. Those are the
seeds that matter: Hive is image search, so it finds *the image* reposted or altered. The enrolment
`ReferenceImage` is a selfie taken thirty seconds earlier that nobody has ever reposted, so
searching it finds nothing, correctly, forever.

Without attribution there is no way to know which enrolled person a photo should be a seed *for*.
That is the whole dependency.

---

## Scope — read this before designing

**In scope:** detect faces, match each against `identity-v1`, register a seed per matched person.

**Not in scope, and do not build:**

- `discovered-v1`, clustering, cluster claims, cluster deletion
- the shield rule / photo protection / coverage arithmetic
- anything in `ARCHITECTURE.md` describing adjudication, crop fetching, or evidence export

The proxy has a full photo-protection surface built against fakes. It stays flagged off. This task
builds only what the seed rule needs.

---

## Invariant #1 needs narrowing — stop and confirm before coding

`CLAUDE.md` §4 #1 forbids `SearchFacesByImage` outright, and CI has a grep gate that fails the build
on it. That rule exists because the old system derived **identity** from a search score, with
thresholds varying 90/95/99 across call sites, overwriting a returning user's `user_ref` on a miss.

This task requires that exact API call, for a different purpose.

The distinction, and it is a real one:

```
FORBIDDEN  establishing WHO SOMEONE IS from a similarity score.
           Enrolment identity comes from the authenticated request. Always.

PERMITTED  attributing a face in a THIRD-PARTY PHOTO to an ALREADY-ENROLLED
           person, where every candidate is a user_ref the caller supplied and
           a non-match is a first-class, harmless outcome.
```

The second cannot corrupt an identity: no `user_ref` is created, none is reassigned, and the worst
case is a seed not registered.

Propose the reworded #1 and the narrowed grep gate — scoped to the enrolment path rather than the
whole of `src/` — **and stop for review before writing code.** Do not widen the gate on your own
initiative; that rule has caught four real contradictions already.

---

## Migration

Number it to follow whatever is currently highest in `migrations/`.

```sql
CREATE TABLE attribution_runs (
  run_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  photo_ref         TEXT NOT NULL,      -- proxy's photo_id, opaque to us
  requested_by      UUID NOT NULL,      -- owner user_ref
  candidate_count   INT NOT NULL,
  match_threshold   NUMERIC(5,2) NOT NULL,
  max_candidates    INT NOT NULL,
  model_id          TEXT NOT NULL,
  faces_detected    INT,
  faces_attributed  INT,
  status            TEXT NOT NULL DEFAULT 'running',
  error_detail      TEXT,
  started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at      TIMESTAMPTZ
);

CREATE INDEX attribution_runs_photo_idx
  ON attribution_runs (photo_ref, started_at DESC);

CREATE TABLE attributed_faces (
  face_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id             UUID NOT NULL REFERENCES attribution_runs(run_id) ON DELETE CASCADE,
  face_index         INT NOT NULL,
  bbox               JSONB NOT NULL,     -- {x,y,w,h} normalised 0-1
  detect_confidence  NUMERIC(5,2) NOT NULL,
  -- NULL means "a face we could not attribute to any candidate". First-class
  -- and expected — it is most faces in most photos.
  resolved_user_ref  UUID,
  match_score        NUMERIC(5,2),
  model_id           TEXT NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ((resolved_user_ref IS NULL) = (match_score IS NULL))
);

CREATE UNIQUE INDEX attributed_faces_uniq ON attributed_faces (run_id, face_index);
CREATE INDEX attributed_faces_person_idx ON attributed_faces (resolved_user_ref)
  WHERE resolved_user_ref IS NOT NULL;

-- A seed now records which face produced it.
ALTER TABLE search_seeds
  ADD COLUMN attributed_face_id UUID REFERENCES attributed_faces(face_id);
```

The `CHECK` pairing mirrors the proxy's own `media.photo_faces` constraint. Keep it: a match score
without a person, or a person without a score, is a bug that should fail at the insert.

`detect_confidence` and `match_score` are **different quantities**. The first is "this region is a
face"; the second is "this face is that person". Conflating them is how a confident detection of a
stranger reads as a confident identification.

---

## The endpoint

```
POST /v1/attribute
  {
    photo_ref:        str,        # proxy's photo_id. Opaque to us.
    requested_by:     uuid,       # owner user_ref
    candidate_refs:   [uuid],     # enrolled people who MAY be in this photo
    presigned_get_url: str,       # https only, >= 15 min TTL
    max_candidates:   int,        # >= 2, from config
    match_threshold:  float       # from config
  }
  -> 200 {
       run_id,
       faces: [
         { face_index, bbox, detect_confidence,
           resolved_user_ref | null, match_score | null }
       ],
       seeds_registered: [ { user_ref, seed_id } ]
     }
```

### Sequence

```
1. DetectFaces on the presigned URL. No faces -> 200, empty, run completed.
2. Per face: SearchFacesByImage against identity-v1, MaxFaces = max_candidates,
   FaceMatchThreshold = match_threshold.
3. HOUSEHOLD SCOPING IS A RESULT FILTER, NOT A SEARCH PARAMETER.
   SearchFacesByImage takes only CollectionId, Image, FaceMatchThreshold,
   MaxFaces and QualityFilter — there is no way to restrict the search set.
   Search all of identity-v1, then DISCARD every match whose ExternalImageId
   is absent from candidate_refs.
4. Highest-scoring surviving candidate wins. None -> resolved_user_ref NULL.
5. Per distinct resolved_user_ref, register ONE seed
   (seed_kind='user_supplied', source_object_ref = photo_ref),
   linked to the attributed_face that produced it.
6. Write the run, the faces, and the seeds in ONE transaction.
```

**`max_candidates` must be at least two**, enforced by the config schema. A single-candidate search
silently loses coverage as the collection grows: a stranger outranking the household member returns
only the stranger, the match is discarded at step 3, and the photo never becomes a seed for its own
owner.

**Record `match_threshold`, `max_candidates`, and `model_id` on the run.** A later retune otherwise
makes every historical attribution uninterpretable — same reason `search_runs.threshold_config`
exists.

### What crosses, and what does not

- **Never image bytes.** A presigned GET goes to Rekognition; nothing is fetched into this process,
  nothing is written to disk. There is no S3 client and the IAM role has no `s3:*` grant.
- **Never a face vector.** `resolved_user_ref` is the `ExternalImageId` we ourselves set at
  enrolment. Nothing here reads, copies, or moves a vector.
- **Never a phone number.** `candidate_refs` are opaque UUIDs; we cannot resolve them to people and
  do not try.

### The unattributed face

`resolved_user_ref = NULL` is the common case and must be handled as ordinary. Do not log it as a
warning, do not fail the run, do not report it as an error to the proxy. Most faces in most photos
belong to people who are not enrolled, and that is exactly the outcome the rule intends.

**Store the bbox for every face**, attributed or not. It is provenance for a decision, and the proxy
renders boxes from it.

---

## What goes to Hive: the whole photo

A seed registered for a matched face uses the **full photo**, not a crop of that face.

Hive is image search. It matches *the image*, and a face crop will not match the full photo it came
from — cropping collapses recall to near zero, which defeats the purpose.

The consequence, stated plainly rather than hidden: an unattributed person's face travels with the
photo when it is sent to Hive. Mitigating facts, and they are the reason this is acceptable rather
than merely convenient:

- Screen 16 asks specifically for photos *"you've recently shared on social media"*. The photo is
  already public, and Hive's ~25B-image web index very likely holds it already.
- Hive receives the image to match it, not to identify anyone. No face vector is created for the
  unattributed person, and nothing about them is stored on our side beyond a bbox.

Record this in `PROXY_INTEGRATION.md` so it is a decision on the record rather than an assumption.

---

## The proxy change this replaces

`image_backend/src/media/ports.ts`, `registerSeed`, currently reads:

> *"THE CALLER MUST ONLY CALL THIS FOR A FULLY PROTECTED PHOTO — every detected face resolving to a
> covered person. A photo with one unrecognised face never becomes a seed."*

That gate is photo-level. It protects the right thing — a non-consenting person's face should not
become the *subject* of a search — but it costs more than it needs to: one stranger in frame
discards a valid seed for the owner.

Face-level gating protects the same thing more precisely. The stranger is never attributed, never
gets a seed, never becomes a monitored subject. Only enrolled people are monitored, which was the
actual concern.

Send this section to the proxy team with the contract.

---

## Done when

- the reworded invariant #1 and the narrowed grep gate are **agreed before code exists**
- a photo with one enrolled face and two strangers registers exactly **one** seed
- a photo with two enrolled household members registers exactly **two** seeds, one per person
- a photo with no enrolled faces registers **zero** seeds and returns 200, not an error
- a photo with no faces at all returns 200 with an empty list and a completed run
- a match whose `ExternalImageId` is absent from `candidate_refs` is discarded — assert with a
  planted non-candidate that would otherwise outrank the real one
- `max_candidates < 2` is rejected by the config schema at boot
- run, faces, and seeds commit in one transaction — kill mid-write, confirm none exist
- `attributed_faces` holds a bbox for **every** detected face, including unattributed ones
- the `CHECK` rejects a row with a score and no person, and one with a person and no score
- no S3 client is imported and the IAM role still has no `s3:*` grant
- `grep -rn "phone" src/imageshield/attribution/` returns nothing

Stop when done.

---

## Standing rules

```
- Cite file:line when describing existing behaviour. Mark anything not read
  directly as INFERRED.
- If anything here conflicts with CLAUDE.md §4, STOP AND ASK. This task
  DELIBERATELY conflicts with #1 — propose the narrowing and wait.
- Doc corrections land in the same commit as the code they describe.
- When the task is done, STOP.
```
