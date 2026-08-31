# Face-crop seeds — design

**Date:** 2026-08-31 · **Status:** **APPROVED IN PRINCIPLE, GATED ON A MEASUREMENT.** The owner
answered §7.1 *yes* (stop seeding the full photo) on 2026-08-31 and left §7.2 to my recommendation
(`face_crop`). §7.3 came back "I don't know", which turns §6's measurement from a nice-to-have into
the one thing standing between this and implementation. Nothing built.
**Counterpart:** `../image_backend/docs/superpowers/specs/2026-08-31-weekly-reports-design.md` §7
and `../image_backend/docs/SERVICES-ASKS.md` §11.

## 0. What is being proposed, in one paragraph

On a photo with **two or more detected faces**, each attributed subject's search seed becomes **a
crop of their own face**, and **the full photo stops being a seed**. Single-face photos are
unchanged. The cropping, the persisting and the seed registration all happen in this repo, because
`attribution/crop.py` already makes that crop; the proxy mints the presigned PUT and owns the object.

## 1. Why — and the reason is consent, not detection

The proxy's `src/reports/seeds.ts` records why the seed gate moved from photo-level to face-level:
"the stranger is never attributed, so they never get a seed and never become a monitored subject —
which was the actual concern."

That is true about becoming a **monitored subject**, and it is the right fix for that. It says nothing
about the stranger's face being **transmitted**.

While the full photo is the seed, a person in frame who never consented — a household member with
monitoring off, one who has not enrolled, a passer-by — has their face sent to **Hive and Google on
every scan cycle, indefinitely**. Nothing on either side currently stops it. `search/worker.py`
dispatches the seed URL to whichever adapters are enabled; the adapters take a URL and do not know or
care how many faces are in the image behind it.

This bears on the rules this repo already holds, not just the proxy's:

- **§1: "We do not build search-by-arbitrary-face. A system that takes any face and returns where it
  appears online is a stalking tool."** We are not doing that — the candidate filter in `resolve.py`
  is what prevents it. But we *are*, today, transmitting a non-consenting person's face to two
  third-party search providers, repeatedly, on a weekly cadence. That is not the same harm and it is
  not nothing.
- **§4 #1a's five conditions** are about matches not *influencing* anything. They are satisfied.
  What is not addressed anywhere is what leaves the process in the query.
- **§7.7: "Several reverse-face-search providers explicitly prohibit third-party monitoring
  services."** A query image containing people who never consented is the version of that problem
  with the least defensible story.

Cropping to the subject removes them from the query. **Keeping the full photo alongside a crop would
not** — the face still ships. That is why this is a replacement, not an addition.

### 1.1 A second, smaller reason found on the proxy side

Their `SearchService#dueSeeds` mints the seed URL from `photos.object_key`, one row per
`(photo_id, person_id)` seed. So two covered household members in one photo dispatch **the same
object** to both providers — two runs, identical query, double spend, every cycle. Distinct crops end
that. It is a cost argument, not a safety one, and it should not be the reason we do this.

## 2. What changes in `attribution/`

The crop already exists. `attribution/rekognition.py#search_face` calls
`crop_to_face(image, face.bbox)` for every detected face, searches it, and **discards it**. The
proposal is to keep exactly one of those crops per attributed subject.

Sequence, inside `attribute_photo` (`attribution/service.py`), after `resolve_face` and before
`store.record_run`:

1. `distinct_attributed(faces)` already yields one `(user_ref, face)` pair per subject. That pair is
   the crop we want: `face.bbox` is the region, `user_ref` is the owner.
2. Only when `len(detected) >= 2`. On a single-face photo the photo *is* the subject's image and a
   crop buys nothing.
3. Re-crop from the in-memory `image` (`crop_to_face`, the same call the search used). Do **not**
   thread the search's crop through the provider interface to reach it — that would put a persistence
   concern into an adapter whose job is a Rekognition call, and `crop.py` is deterministic, so
   re-cropping costs one Pillow operation and keeps the seam clean.
4. `CropTooSmall` is **not** an error here. A face too small to search is already an unattributed
   face, so it never reaches this path; a face that searched fine but crops small on the second call
   cannot happen (same bytes, same bbox, same function). If it somehow does, skip the crop and fall
   back to §5's rule.
5. PUT the crop through the proxy-minted URL with `HttpxObjectUploader` — the same object this repo
   already uses for the liveness `ReferenceImage` — and discard the buffer.
6. Register the seed with `source_object_ref` = the **opaque crop ref the proxy minted**, not the PUT
   URL. Migration 0011's rule stands: "an opaque durable reference, never a URL. The presigned GET
   this run was given expires; the seed must not carry it."

**INVARIANTS #9 is untouched.** No bytes are persisted *by this repo*: the crop goes out through a
presigned PUT into the proxy's bucket and the buffer dies with the request, which is precisely what
hard rule 3 describes. **Pillow gains no fourth call site** — `crop_to_face` already exists and is
already sanctioned; this calls it again from `service.py`, which is the module that already owns the
attribution sequence.

## 3. How the presigned URL arrives — no new proxy endpoint

Follow `POST /v1/liveness/{sid}/result` exactly, which already takes `reference_put_url` and a
**list** of `audit_put_urls` minted in advance by the proxy.

`AttributeRequest` gains one optional field:

```python
class CropTarget(ServiceModel):
    user_ref: UserRef
    # The proxy's opaque object key for this crop. Stored on the seed;
    # 0011 forbids a URL there.
    crop_ref: str
    # Presigned PUT. A credential: never logged, never stored, never returned.
    crop_put_url: str


class AttributeRequest(ServiceModel):
    photo_ref: str
    requested_by: UserRef
    candidate_refs: list[UserRef]
    presigned_get_url: str
    # ONE PER CANDIDATE, minted before we know which candidates will attribute
    # — the proxy cannot know that in advance, and an unused presigned URL
    # simply expires. Absent or empty = the caller does not want crop seeds,
    # and the full photo is seeded as it is today.
    crop_targets: list[CropTarget] = []
```

**Why one per candidate rather than a callback:** the proxy cannot know which candidates will
attribute until we answer, so it mints for all of them. Minting is free and an unused URL expires.
The alternative — us calling a proxy endpoint mid-request to get a URL — adds a synchronous
dependency in the middle of an operation that is already holding image bytes and doing N Rekognition
searches, for no benefit.

**Validation:** every `crop_put_url` must be absolute `https://`, the same `_https_only` validator
`presigned_get_url` already carries, and for the same reason — the object behind it is a photograph
of someone's face crossing the public internet. Duplicate `user_ref`s in `crop_targets` are a `422`:
two PUT targets for one subject is a caller bug, and picking one silently would leave the other
object dangling in their bucket forever.

## 4. The `seed_kind` question

`search_seeds.seed_kind` is `enrolment | user_supplied | public_profile` (0001:69, `CHECK`-less TEXT
but a documented vocabulary).

A crop is *derived from* a user-supplied photo, so `user_supplied` is defensible and needs no
migration. But the corpus stops being legible: nothing then distinguishes "the photo they gave us"
from "a region we cut out of it", and the first question anyone asks when calibration looks odd is
which kind of image the provider actually saw.

**Decided: a new value, `face_crop`** (§7.2 — left to me, taken on this reasoning). One migration,
no `CHECK` to widen, and every later question about provider behaviour by seed shape becomes
answerable with a `GROUP BY`.

## 5. Stopping the full-photo seed — the part that actually delivers §1

`store.record_run` currently writes one seed per `distinct_attributed` subject with
`source_object_ref = photo_ref`. The change: **when a crop was successfully persisted for a subject,
that subject's seed is the crop and no photo seed is written for them.**

Two failure modes, and neither may silently fall back to the full photo:

| Case | Behaviour |
|---|---|
| `crop_targets` absent/empty (proxy did not ask) | full photo seeded, exactly as today |
| Single detected face | full photo seeded — the photo *is* the subject's image |
| Crop PUT fails (`UploadError`) | **no seed for that subject**, run still completes, one `audit_log` row. NOT a photo seed. |

That last row is the load-bearing one. Falling back to the full photo on an upload failure would mean
a transient S3 hiccup silently reinstates the exact transmission this design exists to prevent — and
it would do so invisibly, which is worse than not seeding at all. A missing seed is recoverable: the
next `POST /v1/attribute` for that photo registers it. A face sent to Hive is not recoverable.

## 6. What this costs in detection, stated plainly

A verbatim repost of the whole group photo becomes harder to find.

- **Google** maps `partialMatchingImages` (`search/google.py:39`), which exists for exactly the
  crop-to-parent relationship, so it may still surface the original page.
- **Hive** is near-duplicate embedding over whole images. A face crop against an index of full photos
  will probably miss the parent. `search/hive.py`'s `kind` is `image_search` and this is the limit of
  what image search does.

The proxy's spec accepts this on the grounds that their §1 makes consent the constraint and detection
the trade-off, and this repo's §1 says the same thing in the same words.

**Asked on 2026-08-31 whether anyone already knew how Hive behaves on a face crop, the answer was
"I don't know".** So nobody does, and this stops being a footnote: we would be accepting a detection
regression of unknown size in a product where §1 calls a false negative a broken promise. **Measure
before building.**

### 6.1 The measurement, and it is cheaper than I first wrote

An earlier draft of this section called the spike "real spend". That was an overstatement and worth
correcting, because it made a trivial cost sound like a decision:

- **~10 group photos × 2 query shapes × 2 providers = ~40 calls.**
- **Google:** list price 0.003500/call (their `providers` row), so ~20 calls ≈ **$0.07**.
- **Hive:** genuinely unknown, because `hive.cost_per_call_usd` is **still NULL** — deliberately, per
  migration 0009 ("Hive Web Search is contract-priced and no measured figure exists in this repo")
  and CLAUDE.md §7.6. Even at a generous guess this is cents, but the honest statement is that we
  cannot price it, and that is the standing gap 0009's own FOLLOW-UP note names.

So the cost is not the obstacle; the absence of the number is. **Filling
`hive.cost_per_call_usd` is a prerequisite for capping Hive's spend at all** (§7.6: "A budget set
without a cost fails closed"), and this spike is a natural moment to measure it from the dashboard.

### 6.2 Most of the harness already exists

`devtools/harness/server.py` already exposes both providers and already accepts **either an uploaded
file or a URL** (`media: UploadFile | None`, `url: str | None`). So the spike is: crop N photos
locally with `crop_to_face`, POST each crop and each full photo to both endpoints, and diff the
result sets. No new provider client, no app boot, and therefore no argument with
`config.py`'s refusal to run anything but `stub` in development.

**What to record per photo:** whether the parent page appeared at all, which Google section carried
it (`fullMatchingImages` vs `partialMatchingImages` vs `pagesWithMatchingImages`), and for Hive
whether the `matches` path came back non-empty. `raw_payload` verbatim, per §7.2, so a later
recalibration can re-read it.

**The decision rule, fixed before the data arrives** so it cannot be rationalised afterwards: if
crops find the parent page in a clear majority of cases, ship as designed. If crops find materially
less, the consent argument in §1 still stands and the full photo still must not be seeded — but the
product needs to be told the coverage it is buying, and the scope text in §7.1 may need to say so.
**"Crops find less, so keep seeding the full photo" is not an available answer**: that reinstates the
transmission this design exists to prevent.

## 7. The three questions — answered 2026-08-31

1. **Stop seeding the full photo when a crop exists? — YES.** Owner, 2026-08-31. This is the whole
   point: it is what turns the design from a detection tweak into a consent fix. `record_run` writes
   the crop seed and no photo seed for that subject, with the failure rule in §5 (a failed PUT writes
   NO seed, and must never fall back to the photo).
2. **`face_crop` as a new `seed_kind`? — YES, taken as my call.** Left unanswered, so I am proceeding
   on §4's recommendation rather than re-asking: one migration, no `CHECK` to widen, and
   provider-behaviour-by-seed-shape stays answerable with a `GROUP BY`. Reusing `user_supplied` would
   make "the photo they gave us" and "a region we cut out of it" indistinguishable in the corpus,
   which is the first thing anyone wants to separate when calibration looks wrong. **Say the word and
   it reverts to `user_supplied`;** it is one line either way, but it is one line in a migration, so
   it is easier to decide now than after there are rows.
3. **Does anyone know how Hive behaves on a face crop? — "I don't know."** So §6.1's measurement is
   now the gate. It costs cents, not money, and most of the harness already exists.

**Net: the design is settled and implementation is gated on §6, not on agreement.**

## 8. Where this sits in the build order

**Outside it.** §8's numbered order (1–9) is untouched, exactly as the 2026-08-19 protection-score
push and the 2026-08-21 subject-verified-hits work were. `attribution/` is built and this modifies
it; nothing here is a new deployable, a new queue or a new external dependency, so §3's rule 6 does
not apply — but §7.1's warning does, in a way worth restating: this makes our *image search* query a
face, which is **not** the same as making it a face search. A crop of a face searched against an
index of images finds images containing those pixels. It does not find that person elsewhere. The
product promise in §7.1 is unchanged by this and the scope text stays true.
