# Whole-frame blur, sharp face on tap — design

**Date:** 2026-09-02 · **Status:** approved (owner, in-session), building on branch
`whole-frame-blur` · **Amends:** `CLAUDE.md` §1 ("We do not show full images. Face crops, rendered
live, blurred by default, revealed on tap"), `INVARIANTS.md` #23, and the backend's
`docs/CLAUDE.md` §4 rule 4 / P17, all of which justify the preview relay on it carrying a *crop*.
**Builds on:** `2026-08-21-subject-verified-hits-design.md`.

## 0. The decision, on the record

Four product calls were made by the owner on 2026-09-02, with the trade-offs named:

1. **The frame is the WHOLE image, not a face crop.** The subject sees the entire hit image with a
   blur over it, rather than a tight crop of their own face. The reason is decision quality: to
   answer "is this you?" honestly a person needs to recognise the *photo*, not just a face box, and
   a 25%-margin crop strips exactly the context that makes a photo recognisable as theirs.
2. **Every hit, no exception — including `likely_not_subject`.** The named risk was put to the owner
   and reaffirmed: where the face-match ran and FAILED, the image may be a *stranger's*, and this
   design shows a stranger's whole frame (blurred) to another user. This is not a new judgement —
   the 2026-08-21 spec §0.3 weighed the same harm for the crop and chose showing over withholding,
   for the same reason (a deepfake of the subject legitimately fails face-match, and the
   alternative leaves the subject deciding blind). What is new is the frame's *extent*, which
   widens the exposure from a face to a whole scene. Recorded, accepted, mitigated by §1–§3 below;
   not eliminated.
3. **Default is fully blurred — face included.** Opening a card never renders an identifiable face.
   This preserves the consent model that INVARIANTS #23 exists for: nobody is ambushed by their own
   abuse imagery, or by a stranger's, merely by scrolling a report.
4. **The tap sharpens ONLY the face.** There is no state, on any code path, that returns a fully
   unblurred frame. The explicit content of a hit image is never rendered sharp by this system —
   what the subject un-blurs is the one region they need to answer the question.

Call 4 is the load-bearing safety property of this design, and §5 makes it structural rather than
conventional: the endpoint has no parameter that could ask for it.

## 1. The render contract

`POST /v1/crop` on the fetcher (`src/imageshield/fetcher/app.py`) gains a whole-frame mode. Given
the fetched image bytes and the face `bbox`:

| `blur` (request) | Render |
|---|---|
| `true` (default) | The whole frame, downscaled (§3), Gaussian-blurred end to end at a radius proportional to its long edge (§2). No sharp region anywhere. |
| `false` | The same blurred base, with the **sharp face region composited back over it**, inside `bbox` only. Everything outside `bbox` stays blurred. |

The response stays `image/jpeg` with `Cache-Control: no-store, private`. Nothing is written to disk
or cache at any point — INVARIANTS #9 and #10 are untouched and still true.

Note the parameter name `blur` now reads oddly (`blur=false` still returns a mostly-blurred image).
It is kept rather than renamed: the backend passes it through from `reveal`, and renaming it is a
coordinated two-repo deploy for a cosmetic gain. The docstring must say plainly that `blur=false`
means "sharpen the face", not "return the image unblurred".

### 1a. The sharp region is `bbox` plus a small margin of its own

Two regions must not be confused. `crop_to_face` expands the box by `_MARGIN_FRACTION = 0.25`
because a *search seed* wants the face plus surrounding pixels. The composite wants the opposite:
the smallest area that makes a face recognisable, because every pixel it un-blurs is a pixel of the
hit image shown sharp.

So the sharp region is `bbox` grown by its **own, smaller, separately named** fraction — enough that
a tight Rekognition box does not clip the jaw or hairline, and no more. It is a new constant in the
fetcher and must not reuse or import `_MARGIN_FRACTION`, whose value is chosen for a different
purpose and would silently widen the sharp area if it were ever retuned for seeds.

The region is clamped to the image bounds, and the composite is taken from the **downscaled** base
(§3) so the two layers align exactly.

**Consequence for the preview path: `crop_to_face` is no longer called by the fetcher at all.** The
default render needs no crop, and the reveal render crops the *downscaled base*, not the source, by
a different margin. Two knock-ons:

- The `crop_too_small` 400 (`CropTooSmall`) stops applying to this route. A face too small to crop
  usefully still yields a perfectly good fully-blurred frame, and refusing the whole render because
  the sharp region would be tiny would withhold the default view for no safety gain. On a `bbox`
  that degenerates below a usable size, the reveal render returns the blurred frame unchanged
  rather than erroring.
- `UndecodableImage` is still needed and still raised — decoding a full frame can fail exactly as
  decoding a crop could. Only the crop import goes.

## 2. The blur radius MUST scale with the image

`_BLUR_RADIUS` is a constant tuned for a face crop a few hundred pixels wide. Applied unchanged to a
3000px frame it is close to transparent: body, pose and setting read clearly through it. Shipping
this change with a fixed radius would deliver a *weaker* privacy guarantee than the crop it
replaces, while widening what is in frame — the worst combination available.

The radius becomes a fraction of the long edge, floored so a small image still blurs meaningfully.
The fraction is a named constant in the fetcher, not an inline literal (INVARIANTS #1b's reasoning
applies to any tunable, not only thresholds).

## 3. Downscale before blurring

The long edge is capped (target: 1024px) before the blur. Three reasons:

- The backend's relay **buffers** rather than streams, on the stated reasoning that "a crop is tens
  of kilobytes" (`src/reports/preview-client.ts`). Multi-megabyte frames through a buffered relay
  invalidate that reasoning, and its deliberate choice not to stream — a truncated JPEG under a 200
  being a worse answer for a distressed user than an honest empty slot — depends on the payload
  staying small.
- Less resolution is less re-identification detail in the blurred region.
- It is invisible at phone size, which is where this is read.

Downscale happens BEFORE the blur so the radius-to-content ratio is predictable, and the sharp face
composite is taken from the downscaled base so the two layers align exactly.

## 4. A hit with no bbox stays a 404

`unassessed` hits have no `review_tasks.triage->'best_face_bbox'` — nothing ran, so there is no face
to sharpen. These keep today's `404 preview_unavailable` and the existing "we found a photo we could
not check" card (`src/reports/card.ts#ASK_COPY.unchecked`).

Rejected alternative: showing a fully-blurred frame with an inert tap. A button that does nothing
reads as a broken feature, and a blurred unidentifiable frame adds nothing to a decision the card
already asks honestly without an image.

## 5. `crop_to_face` IS NOT MODIFIED

The whole-frame render is a **new function**. `crop_to_face` has three callers and only one of them
is the preview:

| Caller | What it feeds |
|---|---|
| `attribution/crop_upload.py:105` | face-crop **search seeds** (0029) — the bytes uploaded and sent to Hive/Google |
| `attribution/rekognition.py:98` | the candidate crop before `SearchFacesByImage` |
| `fetcher/app.py:211` | the subject preview — and per §1a this call **goes away** |

Changing `crop_to_face` would silently alter what reaches the search providers and what identity
matching runs against. Both are expensive to unwind and neither is in scope. After this change it
has exactly two callers, both in `attribution/`, both untouched — which is a tidier boundary than
it has today, since the preview and the search seeds no longer share a crop whose margin serves
only one of them.

**Pillow gains a fourth call site, named and documented.** *Corrected 2026-09-02, after this spec
was first committed:* the "three call sites, and no more" rule in `CLAUDE.md` §2 is a
**documentation convention, not a test** — `tests/test_boundaries.py` enforces the face-search grep
and the S3 grep, and asserts nothing about Pillow. The first draft of this section claimed a
boundary test would hold the count; no such assertion exists, and planning against it would have
produced a test step for a test that cannot be written.

So the count is a choice rather than a constraint, and the choice is a **new module,
`src/imageshield/fetcher/render.py`**, holding the whole-frame render as pure functions over bytes.
Reasons: it matches how `attribution/crop.py` already separates image algebra from routes,
`fetcher/app.py` is a route module and this is ~40 lines of pixel work, and a pure function is
directly unit-testable without a `TestClient`. `CLAUDE.md` §2 is amended in the same change to say
**four** sites and to name this one — the rule's purpose is that every place decoding untrusted
bytes is known and listed, which naming serves and refusing a file does not.

## 6. What the backend does NOT change

`image_backend` needs **no logic change**. `src/reports/preview-client.ts` relays opaque bytes and
already pins `Cache-Control`; `reveal` already travels as a query parameter and its meaning is
authored on the services side. Its half of this change is documentation only (§7), plus keeping the
`SERVICES_PREVIEW_TIMEOUT` honest if the render measurably slows — a full-frame blur is more work
than a crop blur, and 10s is the current budget.

`preview_expected` in `src/reports/card.ts` keeps its current rule. §4 means the set of hits that
can produce bytes is unchanged by this design.

## 7. Invariants and docs amended in the same change

- **services `CLAUDE.md` §2** — "Pillow has three call sites, and no more" becomes four, naming
  `fetcher/render.py` (§5).
- **services `CLAUDE.md` §1** — "We do not show full images" becomes: full frames are shown to the
  subject only, blurred end to end, with only the face un-blurrable on an explicit per-item tap.
- **services `INVARIANTS.md` #23** — reveal semantics: the tap sharpens the face region, never the
  frame. Add the assertion that no code path returns a fully sharp image.
- **services `INVARIANTS.md` #10** — unchanged, restated for clarity: still rendered in-memory,
  still never cached.
- **backend `docs/CLAUDE.md` §4 rule 4 and P17** — both currently reason from "a crop"; the relay
  argument is unaffected in substance (still no decode in `api`, still nothing persisted) but the
  wording must stop describing the payload as a face crop.
- **`ARCHITECTURE.md` §3.7** — the fetcher's crop route description.

## 8. Testing

Fetcher unit tests, against a synthetic image with a known bbox:

- default (`blur=true`) — no region of the output is sharp; variance inside `bbox` is comparable to
  variance outside it.
- reveal (`blur=false`) — the region inside `bbox` is measurably sharper than the surround, and the
  surround is as blurred as in the default render.
- **no code path returns a fully sharp frame** — the reveal render's outside-`bbox` region never
  matches the source's detail. This is call 4 as a test.
- radius scales: a 4000px input and a 400px input both come back visibly blurred outside the face.
- long edge is capped at the target regardless of input size.
- a missing/malformed bbox still raises `preview_unavailable`, not a fully-blurred frame.
- a `bbox` too small to be a usable sharp region returns the blurred frame, not a 400 (§1a).
- the sharp region uses its own margin constant, not `_MARGIN_FRACTION` — asserted by making the
  sharp area measurably tighter than `crop_to_face` would produce for the same box.
- `crop_to_face`'s existing tests are untouched and still pass — the seed and attribution paths are
  byte-identical.
- **`tests/test_fetcher.py:101` will fail and must be updated**: it asserts
  `blurred.size[0] < 64  # cropped, not the whole frame`, which is exactly the contract this change
  reverses. It is a correct test of the old behaviour, not a bug.
- **The existing test image is a flat colour** (`_png()` fills with one RGB triple), and blurring a
  flat image returns a visually identical image — so every sharp-vs-blurred assertion against it
  would pass vacuously. The tests need a TEXTURED fixture and a variance-based measure.

## 9. Rollout

Services-side only: build, then `fetcher` is the single deployable that changes (`confirm` imports
the crop client but its behaviour is unaffected). No migration. The backend needs no deploy unless
§6's timeout is adjusted.

Dev verification is blocked behind an unrelated live problem: the preview render ceiling is
exhausted for the test account (200/200 in 24h — see the backend session's findings), so a visual
check needs either a fresh window or a raised `preview_daily_render_ceiling` on dev.

## 10. Not doing

- Renaming the `blur` parameter (§1).
- Any change to who may see a hit's pixels — subject only, staff never, unchanged from 2026-08-21.
- Any change to the render ceiling or the per-render audit (#31, #32). Both apply as they are.
- Pixelation or masking instead of Gaussian blur. A stronger obfuscation is a reasonable follow-up
  if the scaled radius proves insufficient in review, but it is a separate decision.
