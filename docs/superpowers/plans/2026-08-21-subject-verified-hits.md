# Subject-Verified Hits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The subject sees a blurred face crop of every triaged hit, answers "is this your photo?", and that answer writes the confirm/reject decision; staff never see hit imagery.

**Architecture:** Confirm enqueue opens to all new hits; `attribution/crop.py` gains a Rekognition transcode; the worker persists the best *detected* bbox even on a failed match. Two new service endpoints (`GET /v1/infringements/{id}/preview`, `POST /v1/infringements/{id}/decision`) plus an admin observer feed; the console loses its pixels path entirely.

**Tech Stack:** FastAPI, psycopg raw SQL, Pillow, httpx, pytest (`REQUIRE_DB=1` for store tests).

**Spec:** `docs/superpowers/specs/2026-08-21-subject-verified-hits-design.md`

## Global Constraints

- Every commit ends with `Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>` — never the Claude trailer.
- Four CI gates must pass at the end: `ruff check .`, mypy (both configured scopes), `REQUIRE_DB=1 pytest`, docker build. While iterating run *targeted* pytest only (full suite is ~7 min idle; see memory).
- Errors always carry the `{error:{code,message,retryable,request_id}}` envelope (`ServiceError` from `imageshield.http.errors` produces it).
- No `bytea`, no persisted image bytes, no bbox/image_url in any `svc` view (none are added).
- Ownership 404s are oracle-safe: `user_ref` in the `WHERE`, one identical `infringement_not_found` for absent and not-yours.
- One threshold per purpose from config; no inline literals.
- All timestamps `TIMESTAMPTZ`; migrations versioned + reversible (`NNNN_*.up.sql` / `.down.sql`).

---

### Task 1: `to_rekognition_jpeg` in attribution/crop.py

**Files:**
- Modify: `src/imageshield/attribution/crop.py`
- Test: `tests/test_attribution_crop.py` (create if crop tests live elsewhere — `grep -l crop_to_face tests/` first and co-locate)

**Interfaces:**
- Produces: `to_rekognition_jpeg(image: bytes) -> bytes` — passthrough for JPEG/PNG, in-memory JPEG re-encode otherwise, raises existing `UndecodableImage` on undecodable bytes. Exported from `imageshield.attribution.crop`.

- [ ] **Step 1: Write failing tests**

```python
import io
import pytest
from PIL import Image
from imageshield.attribution.crop import UndecodableImage, to_rekognition_jpeg


def _image_bytes(fmt: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (128, 40, 200)).save(buffer, format=fmt)
    return buffer.getvalue()


def test_jpeg_passes_through_byte_identical():
    original = _image_bytes("JPEG")
    assert to_rekognition_jpeg(original) is original


def test_png_passes_through_byte_identical():
    original = _image_bytes("PNG")
    assert to_rekognition_jpeg(original) is original


def test_webp_is_reencoded_to_jpeg():
    converted = to_rekognition_jpeg(_image_bytes("WEBP"))
    with Image.open(io.BytesIO(converted)) as opened:
        assert opened.format == "JPEG"
        assert opened.size == (64, 64)  # geometry preserved — bboxes stay valid


def test_undecodable_bytes_raise():
    with pytest.raises(UndecodableImage):
        to_rekognition_jpeg(b"not an image at all")
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_attribution_crop.py -v` → FAIL (`ImportError: to_rekognition_jpeg`)

- [ ] **Step 3: Implement** (append to crop.py; reuse `_JPEG_QUALITY`, existing imports)

```python
# Rekognition accepts JPEG and PNG bytes only. Everything else the web serves
# (WebP above all — the 2026-08-20 weibook hit failed DetectFaces five times on
# one) is re-encoded in memory. convert("RGB") never resizes, so normalised
# bounding boxes computed against the re-encode remain valid for the original.
_REKOGNITION_FORMATS = frozenset({"JPEG", "PNG"})


def to_rekognition_jpeg(image: bytes) -> bytes:
    """Return bytes Rekognition accepts: the input untouched if already
    JPEG/PNG, an in-memory JPEG re-encode otherwise."""
    try:
        with Image.open(io.BytesIO(image)) as opened:
            if opened.format in _REKOGNITION_FORMATS:
                return image
            converted = opened.convert("RGB")
            buffer = io.BytesIO()
            converted.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError, DecompressionBombError) as exc:
        raise UndecodableImage(str(exc)) from exc
```

Also update the module docstring's first line ("Crop a photo… and normalise bytes for Rekognition").

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_attribution_crop.py -v` → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: transcode non-JPEG/PNG bytes before Rekognition (attribution/crop.py)"`

---

### Task 2: Confirm worker — transcode + best-detected-bbox on failed match

**Files:**
- Modify: `src/imageshield/confirm/worker.py` (imports; step 6 block at lines ~372-390; bbox block at ~413-420)
- Test: `tests/test_confirm_worker.py`

**Interfaces:**
- Consumes: `to_rekognition_jpeg` (Task 1).
- Produces: `handle_message` behavior — Rekognition bundle always receives JPEG/PNG; `triage["best_face_bbox"]` is set whenever *any* face was detected (largest face when none matched, `face_match_score` stays `None` there); undecodable-for-Rekognition takes `record_unfetchable`.

- [ ] **Step 1: Write failing tests** (follow the file's existing fake-deps pattern — fakes for store/provider/moderation/fetch already exist there; extend them)

```python
async def test_rekognition_bundle_receives_jpeg_for_webp_hit(...):
    # fetch fake returns WEBP bytes (Task 1's _image_bytes("WEBP") helper shape);
    # provider fake records the bytes it was given.
    ...
    assert Image.open(io.BytesIO(provider_fake.detect_calls[0])).format == "JPEG"

async def test_failed_match_persists_largest_detected_bbox(...):
    # provider fake: detect returns two faces (areas 0.09 and 0.04), search
    # returns no matches for either → resolve_face gives match_score=None.
    ...
    triage = store_fake.triage_calls[0]["triage"]
    assert triage["best_face_bbox"] == {"x": ..., "y": ..., "w": 0.3, "h": 0.3}  # the LARGER face
    assert triage["face_match_score"] is None

async def test_bytes_pillow_cannot_decode_go_unfetchable(...):
    # fetch fake returns b"junk"; dhash raises UndecodableImage already —
    # assert record_unfetchable called, message deleted (returns True), and
    # provider fake NEVER called.
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_confirm_worker.py -k "jpeg or largest or cannot_decode" -v`

- [ ] **Step 3: Implement.** In `handle_message`, immediately before the `# ── 6. The Rekognition bundle` block:

```python
        # Rekognition accepts JPEG/PNG only; the web serves WebP (the
        # 2026-08-20 weibook DLQ message is the proof). pHash above reads the
        # ORIGINAL bytes on purpose — re-encoding first would shift historical
        # phash comparability for no gain.
        try:
            rek_bytes = to_rekognition_jpeg(image_bytes)
        except UndecodableImage:
            await deps.store.record_unfetchable(
                ctx.infringement_id, detail="undecodable for rekognition"
            )
            return True
```

Then replace `image_bytes` with `rek_bytes` in exactly three calls: `deps.provider.detect_faces(...)`, `deps.provider.search_face(...)`, `deps.moderation.assess(...)`. Import `to_rekognition_jpeg` alongside the existing `UndecodableImage` import.

After the `best_score`/`best_bbox` loop (lines ~413-420), add:

```python
        # A failed match still needs a crop target: the subject decides
        # "similar photo — is this you?" from the largest DETECTED face
        # (spec §3). face_match_score stays None, so matched-vs-similar
        # remains distinguishable downstream.
        if best_bbox is None and ranked_faces:
            best_bbox = ranked_faces[0].bbox
```

- [ ] **Step 4: Run to verify pass** — same command → PASS; then `pytest tests/test_confirm_worker.py -v` (whole file)
- [ ] **Step 5: Commit** — `git commit -m "feat: confirm worker transcodes for Rekognition and keeps the detected bbox on failed match"`

---

### Task 3: attribution/service.py transcode

**Files:**
- Modify: `src/imageshield/attribution/service.py:55-57`
- Test: `tests/test_attribution_routes.py` (or the service-level test file `grep -l attribute_photo tests/` finds)

**Interfaces:**
- Consumes: `to_rekognition_jpeg` (Task 1).
- Produces: `/v1/attribute` accepts WebP/HEIC presigned photos; undecodable bytes surface as the existing `AttributionUnavailable` path (route behavior unchanged).

- [ ] **Step 1: Failing test** — fake `PhotoFetcher` returns WebP bytes; fake provider records what `detect_faces` received; assert JPEG. Second test: fetcher returns junk bytes → the route's existing unavailable/failed-run behavior (assert `record_failed_run` called, error raised).
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement** — in `attribute_photo`'s first `try:` block:

```python
        image = await fetcher.fetch(presigned_get_url)
        try:
            image = to_rekognition_jpeg(image)
        except UndecodableImage as exc:
            # Same caller-visible meaning as a fetch that did not yield a
            # usable photo; keeps the route's error contract unchanged.
            raise AttributionUnavailable(f"photo bytes undecodable: {exc}") from exc
        detected = await provider.detect_faces(image)
```

(`UndecodableImage` import from `imageshield.attribution.crop`.)

- [ ] **Step 4: Verify pass**, run the attribution test files.
- [ ] **Step 5: Commit** — `git commit -m "feat: /v1/attribute transcodes presigned photo bytes for Rekognition"`

---

### Task 4: Open the confirm enqueue gate; delete the criteria config

**Files:**
- Modify: `src/imageshield/search/store.py` (`_ENQUEUE_CONFIRM_HITS_SQL` :283-298, `complete_run` protocol :431-439 and impl :682+), `src/imageshield/confirm/models.py` (delete `ConfirmCriteria` :30-38), `src/imageshield/search/runner.py` (:229 and its `confirm` parameter upstream), `src/imageshield/search/worker.py` (where `ConfirmCriteria` is built from config), `src/imageshield/config.py` (delete fields :422-426, validator :597-604, model_validator kinds block :879-885, property :893-899)
- Test: `tests/test_search_store.py` (enqueue tests), `tests/test_config.py`, plus `grep -rl "ConfirmCriteria\|confirm_hive_min_score\|confirm_google_kinds\|hive_min\|google_kinds" src tests` and fix every site.

**Interfaces:**
- Produces: `complete_run(run_id, seed_id, providers_succeeded, *, retier, enqueue_confirm: bool)` — `True` enqueues **every** attestation of this run still `confirm_state='unconfirmed'`, no provider criteria. `ConfirmCriteria` no longer exists.

- [ ] **Step 1: Failing test** — in the existing enqueue test group: a google `page_match` attestation and a hive `provider_score=0.55` attestation both produce outbox rows when `enqueue_confirm=True`; none when `False`.
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement:**

SQL becomes (comment updated to say the gate is deliberately wide — spec §1):

```sql
    INSERT INTO outbox (queue_name, payload)
    SELECT DISTINCT %(queue)s::text,
           jsonb_build_object('event', %(event)s::text, 'id', i.infringement_id)
    FROM infringements i
    JOIN attestations a ON a.infringement_id = i.infringement_id
    WHERE a.last_run_id = %(run_id)s
      AND i.confirm_state = 'unconfirmed'
```

`complete_run`: replace `confirm: ConfirmCriteria | None` with `enqueue_confirm: bool`; the impl executes the enqueue SQL when `enqueue_confirm` (params: `queue`, `event`, `run_id` only). Thread the rename through `runner.py` and `worker.py` (the worker passes `enqueue_confirm=True`; delete the `ConfirmCriteria(...)` construction and its config reads). Delete the four config artifacts listed above. Fix every grep hit including tests and any docstring that names the criteria.

- [ ] **Step 4: Verify** — `pytest tests/test_search_store.py tests/test_config.py -v` and `REQUIRE_DB=1 pytest tests/test_search_store.py -v`; `ruff check .`; mypy.
- [ ] **Step 5: Commit** — `git commit -m "feat: every new hit enqueues for confirm triage; criteria config removed"`

---

### Task 5: Migration 0024 — preview-render audit index

**Files:**
- Create: `migrations/0024_preview_audit_index.up.sql`, `migrations/0024_preview_audit_index.down.sql`
- Test: `REQUIRE_DB=1 pytest tests/test_migrations.py -v` (harness applies all migrations both ways)

- [ ] **Step 1: Write both files**

```sql
-- 0024 up: the preview endpoint's per-user render ceiling (INVARIANTS #32)
-- counts 'preview.rendered' audit rows in a rolling 24h window; this partial
-- index makes that count one range scan instead of a seq scan over all audit.
CREATE INDEX audit_preview_renders_idx
  ON audit_log (subject_ref, occurred_at)
  WHERE action = 'preview.rendered';
```

```sql
-- 0024 down
DROP INDEX audit_preview_renders_idx;
```

- [ ] **Step 2: Verify** — `REQUIRE_DB=1 pytest tests/test_migrations.py -v` → PASS
- [ ] **Step 3: Commit** — `git commit -m "feat: migration 0024 — partial index for preview render ceiling"`

---

### Task 6: Preview module — store + fetcher crop client

**Files:**
- Create: `src/imageshield/preview/__init__.py` (empty), `src/imageshield/preview/store.py`, `src/imageshield/preview/client.py`
- Test: `tests/test_preview_store.py` (REQUIRE_DB fixtures — copy the setup pattern from `tests/test_confirm_store.py`), `tests/test_preview_client.py` (httpx.MockTransport)

**Interfaces:**
- Produces:
  - `PreviewTarget(BaseModel, frozen)`: `image_url: str | None`, `bbox: dict[str, float] | None`
  - `PreviewStore` Protocol / `PostgresPreviewStore(pool)`:
    - `target(infringement_id: UUID, user_ref: UserRef) -> PreviewTarget | None` — `None` = absent/not-yours/quarantined/duplicate (one fact)
    - `renders_last_24h(user_ref: UserRef) -> int`
    - `record_render(user_ref: UserRef, infringement_id: UUID, *, reveal: bool) -> None`
  - `CropUnavailable(Exception)` with `.unrenderable: bool`; `FetcherCropClient(client, *, base_url, token)` with `crop(*, url: str, bbox: dict[str, float], blur: bool) -> bytes`

- [ ] **Step 1: Failing store tests** — seed an infringement + review_task with `triage={"best_face_bbox": {...}}`; assert: owner gets target with bbox; wrong `user_ref` → `None`; `confirm_state='quarantined'` → `None`; no review_task row → `bbox is None`; `record_render` writes an audit row (`actor_type='subject'`, `action='preview.rendered'`, metadata `{"reveal": true}`) and `renders_last_24h` counts it.
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement store** (raw SQL, one connection per method, house style):

```python
_TARGET_SQL = """
    SELECT i.image_url, rt.triage -> 'best_face_bbox'
    FROM infringements i
    LEFT JOIN review_tasks rt ON rt.infringement_id = i.infringement_id
    WHERE i.infringement_id = %(infringement_id)s
      AND i.user_ref = %(user_ref)s
      AND i.confirm_state NOT IN ('quarantined', 'duplicate')
"""

_COUNT_RENDERS_SQL = """
    SELECT count(*) FROM audit_log
    WHERE action = 'preview.rendered'
      AND subject_ref = %(user_ref)s
      AND occurred_at > now() - interval '24 hours'
"""

_RECORD_RENDER_SQL = """
    INSERT INTO audit_log (actor_type, action, subject_ref, resource_id, metadata)
    VALUES ('subject', 'preview.rendered', %(user_ref)s, %(infringement_id)s, %(metadata)s)
"""
```

Module docstring states: the count-then-insert pair is deliberately not atomic — the ceiling is an abuse brake (#32), not an exact quota; two racing requests overshooting by one is acceptable, a lock on audit_log is not.

- [ ] **Step 4: Implement client** — POST `{base_url}/v1/crop` json `{"url", "bbox", "blur"}`, header `X-Fetcher-Token`; 200 → `response.content`; 400 with fetcher codes `crop_too_small`/`not_an_image` → `CropUnavailable(unrenderable=True)`; anything else non-200 → `CropUnavailable(unrenderable=False)`. Never log or echo the token.
- [ ] **Step 5: Verify pass** — `REQUIRE_DB=1 pytest tests/test_preview_store.py -v; pytest tests/test_preview_client.py -v`
- [ ] **Step 6: Commit** — `git commit -m "feat: preview store + fetcher crop client"`

---

### Task 7: `GET /v1/infringements/{id}/preview`

**Files:**
- Modify: `src/imageshield/http/routes/infringements.py`, `src/imageshield/http/deps.py`, `src/imageshield/http/app.py` (lifespan + teardown), `src/imageshield/config.py` (one field)
- Test: `tests/test_preview_routes.py` (pattern-copy `tests/test_infringement_feedback.py`'s app/override setup)

**Interfaces:**
- Consumes: Task 6's store/client; `get_config`.
- Produces: the route below; `deps.get_preview_store`, `deps.get_crop_client`; `Config.preview_daily_render_ceiling: int = 200`.

- [ ] **Step 1: Failing route tests** — with fakes overridden via `app.state`: (a) not-yours and nonexistent both → 404 body `error.code == "infringement_not_found"`, byte-identical bodies; (b) target without bbox → 404 `preview_unavailable`; (c) happy path → 200, `content-type: image/jpeg`, `cache-control: no-store, private`, crop fake received `blur=True`; (d) `?reveal=true` → crop fake received `blur=False`, audit fake recorded `reveal=True`; (e) fake store returning `renders_last_24h() == 200` → 429 `preview_rate_limited` with `retryable: true`; (f) crop client raising `CropUnavailable(unrenderable=False)` → 502 `preview_unavailable_upstream`.
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement route** (same router as feedback):

```python
@router.get("/infringements/{infringement_id}/preview")
async def preview(
    infringement_id: UUID,
    user_ref: UUID = Query(...),
    reveal: bool = Query(False),
    store: PreviewStore = Depends(get_preview_store),
    crop_client: FetcherCropClient = Depends(get_crop_client),
    cfg: Config = Depends(get_config),
) -> Response:
    subject = parse_user_ref(user_ref)
    target = await store.target(infringement_id, subject)
    if target is None:
        # Not there, not theirs, or invisible (quarantined/duplicate). One
        # answer for all — the feedback route's oracle discipline, verbatim.
        raise ServiceError(404, "infringement_not_found",
                           "No such infringement for this user_ref.", retryable=False)
    if target.image_url is None or target.bbox is None:
        raise ServiceError(404, "preview_unavailable",
                           "No renderable crop for this hit yet.", retryable=False)
    if await store.renders_last_24h(subject) >= cfg.preview_daily_render_ceiling:
        raise ServiceError(429, "preview_rate_limited",
                           "Preview render ceiling reached for this user.", retryable=True)
    # Audit BEFORE the render (INVARIANTS #31): a render that then fails
    # upstream still shows an attempt against the ceiling.
    await store.record_render(subject, infringement_id, reveal=reveal)
    try:
        content = await crop_client.crop(url=target.image_url, bbox=target.bbox, blur=not reveal)
    except CropUnavailable as exc:
        if exc.unrenderable:
            raise ServiceError(404, "preview_unavailable",
                               "No renderable crop for this hit.", retryable=False) from exc
        raise ServiceError(502, "preview_unavailable_upstream",
                           "Crop render failed upstream.", retryable=True) from exc
    return Response(content=content, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store, private"})
```

Wire deps (`_required_state` pattern) and lifespan:

```python
    if getattr(app.state, "preview_store", None) is None:
        app.state.preview_store = PostgresPreviewStore(pool)
    if getattr(app.state, "crop_client", None) is None:
        app.state.crop_http_client = httpx.AsyncClient(timeout=15.0)
        app.state.crop_client = FetcherCropClient(
            app.state.crop_http_client, base_url=cfg.fetcher_base_url, token=cfg.fetcher_token
        )
```

(teardown: `aclose()` next to `photo_http_client`'s). Config field beside the other score/confirm knobs, with a positive-int validator matching the file's existing pattern.

- [ ] **Step 4: Verify pass**; also `pytest tests/test_error_envelope.py -v` (new codes ride the envelope).
- [ ] **Step 5: Commit** — `git commit -m "feat: subject preview endpoint — blurred face crop, audited, rate-limited"`

---

### Task 8: `subject_decide` in review/store.py

**Files:**
- Modify: `src/imageshield/review/store.py`
- Test: `tests/test_review_store.py` (or wherever `PostgresReviewStore.decide` is tested — `grep -l subject_decide\|"def decide" tests/`; REQUIRE_DB)

**Interfaces:**
- Produces: `SUBJECT_DECIDED_ACTION = "review.subject_decided"`; `SubjectDecisionOutcome(BaseModel, frozen)`: `infringement_id: UUID`, `decision: str`, `severity: str | None`, `outcome: Literal["decided", "replay", "conflict"]`; `ReviewStore.subject_decide(infringement_id: UUID, *, user_ref: UserRef, decision: str) -> SubjectDecisionOutcome | None` (`None` = absent/not-yours/quarantined/duplicate).

- [ ] **Step 1: Failing store tests** — seed infringement (+ pending review_task): (a) `confirmed` from `machine_triaged` → infringement `confirm_state='confirmed'`, `confirm_decided_by='subject'`, severity untouched, review_task `status='decided', decided_by='subject'`, audit row action `review.subject_decided` with `{"decision","severity","source_domain"}`, outcome `decided`; (b) `rejected` also sets `status='dismissed_not_me'`; (c) repeat same call → outcome `replay`, no second audit row; (d) opposite decision after (a) → `conflict`, nothing written; (e) operator-decided row (decided_by='ops@x') → `conflict`; (f) wrong user_ref → `None`; (g) `quarantined` → `None`; (h) works from `unconfirmed` (no review_task row — step 4 updates zero rows, no error).
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement** — one transaction, mirroring `decide`:

```python
_LOCK_INFRINGEMENT_SUBJECT_SQL = """
    SELECT i.confirm_state, i.confirm_decided_by, i.severity, cu.source_domain
    FROM infringements i
    JOIN content_urls cu ON cu.url_hash = i.url_hash
    WHERE i.infringement_id = %(infringement_id)s AND i.user_ref = %(user_ref)s
    FOR UPDATE OF i
"""

_SUBJECT_DECIDE_INFRINGEMENT_SQL = """
    UPDATE infringements
    SET confirm_state = %(confirm_state)s,
        confirm_decided_by = 'subject',
        confirm_decided_at = now(),
        status = CASE WHEN %(confirm_state)s = 'rejected'
                      THEN 'dismissed_not_me' ELSE status END
    WHERE infringement_id = %(infringement_id)s
    RETURNING severity
"""

_SUBJECT_DECIDE_TASK_SQL = """
    UPDATE review_tasks
    SET status = 'decided', decision = %(decision)s,
        decided_by = 'subject', decided_at = now()
    WHERE infringement_id = %(infringement_id)s AND status = 'pending'
"""

_SUBJECT_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, subject_ref, resource_id, metadata)
    VALUES ('subject', %(action)s, %(subject_ref)s, %(resource_id)s, %(metadata)s)
"""
```

Logic after the lock: row `None` or `confirm_state in ('quarantined','duplicate')` → return `None`. `confirm_state in ('confirmed','rejected')` → `replay` iff `confirm_decided_by == 'subject' and confirm_state == decision` (return stored severity), else `conflict`; neither writes anything. Otherwise write all three statements and return `decided`. Docstring states the #19 lineage: the subject is the deciding human (spec §0.1); a subject can never overturn an operator, and v1 has no re-decide.

- [ ] **Step 4: Verify** — `REQUIRE_DB=1 pytest <the store test file> -v`
- [ ] **Step 5: Commit** — `git commit -m "feat: subject_decide — the subject's answer writes the confirm decision"`

---

### Task 9: `POST /v1/infringements/{id}/decision`

**Files:**
- Modify: `src/imageshield/http/routes/infringements.py`, `src/imageshield/http/models.py`
- Test: `tests/test_subject_decision_routes.py` (pattern-copy `tests/test_infringement_feedback.py`)

**Interfaces:**
- Consumes: `subject_decide` (Task 8), `get_review_store`, `get_score_store`.
- Produces: models `SubjectDecisionRequest(ServiceModel)`: `user_ref: UserRef`, `decision: Literal["confirmed", "rejected"]`; `SubjectDecisionResponse(BaseModel)`: `infringement_id: UUID`, `decision: str`, `severity: str | None`, `idempotent_replay: bool`.

- [ ] **Step 1: Failing route tests** — (a) happy confirm → 200, fake recompute called with `cause_kind="subject_decision"`; (b) replay → 200 `idempotent_replay: true`, recompute NOT called again; (c) conflict → 409 `decision_conflict`, `retryable: false`; (d) `None` → 404 `infringement_not_found`; (e) `decision: "uncertain"` → 422 (Literal rejects); (f) extra body field → 422 (`extra='forbid'`); (g) recompute raising → still 200 (swallow-and-log).
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement:**

```python
@router.post("/infringements/{infringement_id}/decision")
async def subject_decision(
    infringement_id: UUID,
    body: SubjectDecisionRequest,
    review_store: ReviewStore = Depends(get_review_store),
    score_store: ScoreStore = Depends(get_score_store),
) -> SubjectDecisionResponse:
    outcome = await review_store.subject_decide(
        infringement_id, user_ref=body.user_ref, decision=body.decision
    )
    if outcome is None:
        raise ServiceError(404, "infringement_not_found",
                           "No such infringement for this user_ref.", retryable=False)
    if outcome.outcome == "conflict":
        raise ServiceError(409, "decision_conflict",
                           "This hit already carries a different decision.", retryable=False)
    log.info("infringement.subject_decided", infringement_id=str(infringement_id),
             decision=outcome.decision, replay=outcome.outcome == "replay")
    if outcome.outcome == "decided":
        try:
            await score_store.recompute(body.user_ref, cause_kind="subject_decision",
                                        cause_ref=str(infringement_id))
        except Exception:  # deliberate: the decision already committed; tick will heal
            log.warning("score.recompute_failed", user_ref=str(body.user_ref),
                        cause="subject_decision")
    return SubjectDecisionResponse(
        infringement_id=infringement_id, decision=outcome.decision,
        severity=outcome.severity, idempotent_replay=outcome.outcome == "replay",
    )
```

- [ ] **Step 4: Verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat: subject decision endpoint — yes/not-me writes confirm/reject"`

---

### Task 10: Admin observer feed

**Files:**
- Modify: `src/imageshield/review/store.py` (one read method), `src/imageshield/http/routes/admin_review.py`
- Test: extend `tests/test_admin_review_routes.py` + the review-store test file

**Interfaces:**
- Produces: `ReviewStore.subject_decisions(*, limit: int) -> tuple[dict[str, Any], ...]` (each: `occurred_at`, `user_ref`, `infringement_id`, `decision`, `severity`, `source_domain` — unpacked from the audit metadata); `ReviewStore.open_hits(*, limit: int) -> tuple[dict[str, Any], ...]` (each: `user_ref`, `infringement_id`, `confirm_state`, `severity`, `source_domain`, `first_seen_at` — every hit still awaiting an answer; **the control room always sees THAT a person has a hit**, never its pixels); routes `GET /v1/admin/review/subject-decisions?limit=` → `{"decisions": [...]}` and `GET /v1/admin/review/open-hits?limit=` → `{"hits": [...]}` (both tokens, like the rest of the router).

- [ ] **Step 1: Failing tests** — store: two subject decisions → newest first, fields unpacked; `open_hits` returns `unconfirmed` + `machine_triaged` rows (never quarantined/duplicate/decided), newest first; routes: fake store, assert shapes + `limit` clamped by `Query(50, ge=1, le=500)`.
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement** — decisions SQL `SELECT occurred_at, subject_ref, resource_id, metadata FROM audit_log WHERE action = 'review.subject_decided' ORDER BY occurred_at DESC LIMIT %(limit)s` (operator page, tiny N — no index until it hurts); open-hits SQL:

```sql
    SELECT i.user_ref, i.infringement_id, i.confirm_state, i.severity,
           cu.source_domain, i.first_seen_at
    FROM infringements i
    JOIN content_urls cu ON cu.url_hash = i.url_hash
    WHERE i.confirm_state IN ('unconfirmed', 'machine_triaged')
    ORDER BY i.first_seen_at DESC
    LIMIT %(limit)s
```

Routes mirror `queue_depth`'s shape.
- [ ] **Step 4: Verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat: admin subject-decisions observer feed"`

---

### Task 11: Console — remove pixels, add the observer panel

**Files:**
- Modify: `src/imageshield/console/app.py` (delete `/crop` route :129-145 + its `get_fetcher_client` dep + fetcher wiring in the console lifespan; add `/decisions` route), `src/imageshield/console/client.py` (delete `FetcherClient`; add `ServicesClient.subject_decisions`), `src/imageshield/console/templates/review.html` (delete the crop `<img>`/reveal block :13-21, leave the metadata card), `src/imageshield/console/templates/base.html` (nav link), `src/imageshield/console/config.py` (delete fetcher fields), `infra/ecs/imageshield-dev-console.json` (delete the `FETCHER_BASE_URL` env — it has none today — and the `FETCHER_TOKEN` secret entry)
- Create: `src/imageshield/console/templates/decisions.html`
- Test: `tests/test_console.py`, `tests/test_ecs_task_defs.py`

**Interfaces:**
- Consumes: Task 10's admin routes (`subject-decisions` AND `open-hits`).
- Produces: console `GET /decisions` page with an **Open hits** section (that a person has a hit is always visible) above the decided feed; **no** console route returns image bytes anywhere.

- [ ] **Step 1: Failing tests** — (a) `GET /crop` → 404 (route gone); (b) `GET /decisions` renders open-hit rows from a fake `services_client.open_hits()` AND decided rows from `services_client.subject_decisions()`, with `ncii_suspected`/`explicit_unmatched` rows carrying a `class="flag"` marker; (c) review page renders WITHOUT any `<img>` (assert `b"<img" not in response.content`); (d) task-def test passes with the console secret removed.
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement.** `ServicesClient.subject_decisions(limit=50)` GETs `/v1/admin/review/subject-decisions`; `ServicesClient.open_hits(limit=50)` GETs `/v1/admin/review/open-hits`. `decisions.html` gets an "Open hits — awaiting the subject" table (When found / Person / State / Severity / Domain) above the decided table:

```html
{% extends "base.html" %}
{% block title %}Subject decisions - ImageShield Console{% endblock %}
{% block content %}
<h1>Subject decisions</h1>
<p class="muted">Decided by the person themselves. Staff see metadata only — never imagery
(spec 2026-08-21 §0.2). Explicit-severity confirmations are takedown-campaign candidates.</p>
<table>
  <tr><th>When</th><th>Decision</th><th>Severity</th><th>Domain</th><th>Infringement</th></tr>
  {% for d in decisions %}
  <tr {% if d.severity in ("ncii_suspected", "explicit_unmatched") %}class="flag"{% endif %}>
    <td>{{ d.occurred_at }}</td><td>{{ d.decision }}</td><td>{{ d.severity }}</td>
    <td>{{ d.source_domain }}</td><td>{{ d.infringement_id }}</td>
  </tr>
  {% endfor %}
</table>
{% endblock %}
```

`review.html`'s deleted block is replaced with `<p class="muted">Imagery is subject-only (spec 2026-08-21). Decide from the metadata above, or leave for the subject.</p>`.

- [ ] **Step 4: Verify** — `pytest tests/test_console.py tests/test_ecs_task_defs.py -v`
- [ ] **Step 5: Commit** — `git commit -m "feat: console goes metadata-only — crop path removed, subject-decisions panel added"`

---

### Task 12: Documentation amendments (same-PR rule)

**Files:** `INVARIANTS.md`, `CLAUDE.md`, `ARCHITECTURE.md`, `PROXY_INTEGRATION.md`, `docs/prompts/BACKEND-SCORE-SURFACE.md`, `docs/OPERATIONS.md`, `src/imageshield/fetcher/app.py` (docstring only)

- [ ] **Step 1: Apply the spec §7 list, verbatim intent:**
  - INVARIANTS #19: append "The subject is a valid deciding human for hits on their own likeness (`confirm_decided_by = 'subject'`). A blurred face crop may be shown to the subject — and only the subject — for the purpose of deciding; staff surfaces never render hit imagery."
  - INVARIANTS #23: scope "never in a list view" to un-blurred crops; blurred previews render only on the hit's own card; reveal stays per-item explicit tap.
  - INVARIANTS #45: append "A subject **decision** is not feedback: their `confirmed` moves Exposure exactly as an operator confirm would (`cause_kind='subject_decision'`). The no-lowering rule binds the four feedback signals, not the decision lane."
  - INVARIANTS #47: append "…the *subject* now can, via `subject_decide` — machine triage still cannot."
  - CLAUDE.md §2 Pillow paragraph: crop.py "cropping a candidate face … **and normalising bytes to JPEG for Rekognition**"; §4 #19 working-set summary gains the subject-decider sentence; §6 gains a dated note: "**2026-08-21 — subject-verified hits, sanctioned.** See `docs/superpowers/specs/2026-08-21-subject-verified-hits-design.md`: subject decides, staff never see imagery, enqueue gate open, preview + decision endpoints."
  - ARCHITECTURE.md §3.10 (or the review section that exists): review is the exception path (quarantine + override); the fetcher's callers are the confirm worker and the services preview endpoint (console removed).
  - PROXY_INTEGRATION.md: document both endpoints (shapes from Tasks 7/9, including the 404/409/429 codes and the `no-store` contract), mark `GET /v1/hits/{hit_id}/crop` as superseded by `/preview`, and replace the "only confirmed presents as a finding" presentation rule with: triaged hits carry the ask-card ("is this your photo?" on `machine_triaged`, copy keyed on `face_match_score` NULL → "similar photo"); `confirmed` presents as a finding; quarantined/duplicate never appear.
  - BACKEND-SCORE-SURFACE.md: point its presentation-rule paragraph at PROXY_INTEGRATION's updated rule.
  - OPERATIONS.md: subject-decisions feed section (what the flags mean, takedown-campaign handoff); quarantine runbook explicitly unchanged.
  - fetcher/app.py `require_fetcher_token` docstring: "exactly one caller" → "two callers (the confirm worker and the services preview endpoint)".
- [ ] **Step 2: `grep -rn "CONFIRM_HIVE_MIN_SCORE\|CONFIRM_GOOGLE_KINDS" docs *.md` — scrub stale references.**
- [ ] **Step 3: Commit** — `git commit -m "docs: subject-verified hits — invariants #19/#23/#45/#47 amendments + contract docs"`

---

### Task 13: Full gates

- [ ] `ruff check .` → clean
- [ ] mypy, both configured scopes (see CI workflow for the exact two commands) → clean
- [ ] `REQUIRE_DB=1 pytest` (full suite, once — capture output)
- [ ] `docker build` (the CI build step's invocation) → succeeds
- [ ] Fix fallout, commit as `fix:` commits; re-run only the failed gate.
