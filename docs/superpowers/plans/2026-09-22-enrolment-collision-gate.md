# Enrolment Collision Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refuse to index a liveness frame that already belongs to a different `user_ref` — `409 identity_conflict`, nothing written — so an owner (or anyone already enrolled) cannot become an on-device member.

**Architecture:** One new module, `enrolment/collision.py`, runs `SearchFacesByImage` on the reference frame BEFORE `IndexFaces` and returns `Collision | None`; it imports no store and cannot write. The route consumes the session and records the conflict (`enrolment_conflicts`, migration 0036) in one transaction, then raises. INVARIANTS #1 is reworded ("a search may refuse, never assign") and the boundary tests enforce the module's inability to write.

**Tech Stack:** Python 3.11+, FastAPI, psycopg 3 raw SQL, boto3 Rekognition, pytest + pytest-postgresql (`throwaway_db`), structlog.

**Spec:** `docs/superpowers/specs/2026-09-22-enrolment-collision-gate-design.md`

## Global Constraints

- Every inbound body stays a pydantic model with `extra='forbid'`; no new request fields.
- One threshold per purpose (INVARIANTS #1b): the gate reads `enrolment_collision_threshold` and nothing else.
- No inline numeric literals for tunables — `enrolment_collision_max_faces` is config with a default of `5`.
- `user_ref` comes from the session row only. The collision module returns a refusal, never an identity.
- Fail CLOSED: a search failure is `503 face_index_unavailable`, nothing written, nothing indexed.
- The matched `user_ref` NEVER appears in an HTTP response; only `conflict_id` does.
- Migrations are an up/down pair, table grants go to the module role `identity_rw` (0015 pattern), no grant to `imageshield_proxy_ro`.
- Run pytest DB suites one session at a time (`tests/db.py` throwaway databases share one compose Postgres). `ruff format` only files this plan creates.
- Every commit ends with `Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>` and no other trailer.
- Work on `main` of `image_flashbacklabs`. Two unrelated untracked files (`scripts/refresh-demo-image-urls.sh`, `scripts/sql/`) are someone else's — never `git add -A`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/imageshield/config.py` (modify) | `enrolment_collision_max_faces: int = 5`; rewrite the collision-threshold comment (it now HAS a reader) |
| `src/imageshield/enrolment/models.py` (modify) | `IDENTITY_CONFLICT_REASON`, `FaceHit`, `FaceSearchResult`, `Collision`, `EnrolmentConflictRow` |
| `src/imageshield/enrolment/collision.py` (create) | `FaceSearcher` protocol + `collision_check()` — the ONE place face search may run in the enrolment path; imports no store |
| `src/imageshield/enrolment/faceindex.py` (modify) | `RekognitionFaceIndex.search_face()`; `FaceIndex` protocol gains `search_face` |
| `migrations/0036_enrolment_conflicts.up.sql` / `.down.sql` (create) | the provenance table + grants |
| `src/imageshield/liveness/store.py` (modify) | `finalize_conflict()` (one transaction) and `get_conflict()` |
| `src/imageshield/http/errors.py` (modify) | `ServiceError(..., extra=...)` so an envelope can carry `conflict_id` |
| `src/imageshield/http/routes/liveness.py` (modify) | gate before `IndexFaces`; conflict replay on same key; docstring |
| `tests/test_boundaries.py` (modify) | the exemption for `enrolment/collision.py` + two new structural tests |
| `tests/test_collision.py` (create) | unit tests for `collision_check` and `search_face` |
| `tests/test_liveness_routes.py` (modify) | `FakeFaceIndex.search_face`; seven route tests |
| `tests/test_enrolment_store.py` (modify) | `finalize_conflict` DB tests |
| `tests/test_config.py` (modify) | docstring of the unread-fields test; `max_faces` validation |
| `INVARIANTS.md`, `CLAUDE.md`, `PROXY_INTEGRATION.md`, `.env.example` (modify) | the reworded rule, the new 409 |

---

### Task 1: Config — the threshold gains a reader, and a page size

**Files:**
- Modify: `src/imageshield/config.py:138-143` (comment + new field), validators near `:595`
- Modify: `.env.example:47-50`
- Test: `tests/test_config.py:389-395`

**Interfaces:**
- Produces: `Config.enrolment_collision_threshold: float` (existing, 0–100), `Config.enrolment_collision_max_faces: int` (new, default 5, must be ≥ 1).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_collision_max_faces_defaults_to_a_page_and_must_be_positive() -> None:
    """The gate asks 'is anyone ELSE above threshold' — one page answers it.
    Zero would search for nobody and pass every frame: a silent fail-open."""
    assert make_config().enrolment_collision_max_faces == 5
    with pytest.raises(ValidationError):
        make_config(enrolment_collision_max_faces=0)
```

Also rewrite the docstring of `test_unread_fields_are_still_validated` (line 390) to:

```python
    """DISCOVERED_COLLECTION has no reader in v1. ENROLMENT_COLLISION_THRESHOLD
    gained one on 2026-09-22 (enrolment/collision.py). Either way a blank or
    out-of-range value must refuse to boot."""
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd image_flashbacklabs && .venv/Scripts/python -m pytest tests/test_config.py::test_collision_max_faces_defaults_to_a_page_and_must_be_positive -q`
Expected: FAIL — `Config` has no field `enrolment_collision_max_faces`.

- [ ] **Step 3: Implement**

In `config.py`, replace the block at lines 138–143 with:

```python
    # enrolment_collision_threshold: the ONE face search allowed in the
    # enrolment path (enrolment/collision.py, spec 2026-09-22) refuses to index
    # a frame that matches a DIFFERENT user_ref at or above this. It may block
    # an enrolment; it can never assign one — INVARIANTS #1 as reworded that
    # day. Before 2026-09-22 this field was declared and unread, and the
    # comment here said the check must never exist; that reasoning was right
    # about a score CHOOSING a user_ref and wrong about a score REFUSING one.
    enrolment_collision_threshold: float
    # How many matches the collision search asks for. The question is "is
    # anyone else above threshold", and one page answers it. A default, so
    # the deployed env blocks need not change; still config, not a literal.
    enrolment_collision_max_faces: int = 5
```

Add the field name to the positive-int validator list (find the `@field_validator(... "attribution_max_candidates" ...)` or the nearest `_positive_int` validator around line 594 and append `"enrolment_collision_max_faces"`). If no positive-int validator exists, add:

```python
    @field_validator("enrolment_collision_max_faces")
    @classmethod
    def _at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be at least 1")
        return value
```

In `.env.example` lines 47–50 replace the comment with:

```
# The enrolment collision gate (enrolment/collision.py, 2026-09-22): a liveness
# frame matching a DIFFERENT user_ref at or above this is refused with
# 409 identity_conflict and nothing is indexed. 97, not 99: same-person live
# frames often score 97–99, and a false refusal is a retake while a false pass
# is a permanently duplicated identity.
ENROLMENT_COLLISION_THRESHOLD=97
# Matches requested by that search (default 5 — one page answers the question).
# ENROLMENT_COLLISION_MAX_FACES=5
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_config.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/imageshield/config.py .env.example tests/test_config.py
git commit -m "config: the enrolment collision threshold gains a reader, plus a page size

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 2: Models and the collision module (pure logic)

**Files:**
- Modify: `src/imageshield/enrolment/models.py`
- Create: `src/imageshield/enrolment/collision.py`
- Test: `tests/test_collision.py`

**Interfaces:**
- Produces:
  ```python
  IDENTITY_CONFLICT_REASON = "identity_conflict"
  @dataclass(frozen=True, slots=True) class FaceHit: external_image_id: str; similarity: float; face_id: str
  @dataclass(frozen=True, slots=True) class FaceSearchResult: hits: tuple[FaceHit, ...]; model_id: str
  @dataclass(frozen=True, slots=True) class Collision: matched_user_ref: UserRef; similarity: float; model_id: str; threshold_used: float
  class FaceSearcher(Protocol):
      async def search_face(self, *, collection_id: str, image_bytes: bytes, threshold: float, max_faces: int) -> FaceSearchResult: ...
  async def collision_check(searcher: FaceSearcher, cfg: Config, own_ref: UserRef, image_bytes: bytes) -> Collision | None
  ```

- [ ] **Step 1: Write the failing tests**

Create `tests/test_collision.py`:

```python
"""The one face search allowed in the enrolment path can REFUSE, never ASSIGN."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from imageshield.enrolment.collision import collision_check
from imageshield.enrolment.models import FaceHit, FaceIndexUnavailable, FaceSearchResult
from imageshield.types import UserRef
from tests.conftest import make_config


class FakeSearcher:
    def __init__(self, result: FaceSearchResult | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def search_face(
        self, *, collection_id: str, image_bytes: bytes, threshold: float, max_faces: int
    ) -> FaceSearchResult:
        self.calls.append(
            {
                "collection_id": collection_id,
                "image_bytes": image_bytes,
                "threshold": threshold,
                "max_faces": max_faces,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def hit(ref: Any, similarity: float) -> FaceHit:
    return FaceHit(external_image_id=str(ref), similarity=similarity, face_id=f"f-{similarity}")


def result(*hits: FaceHit) -> FaceSearchResult:
    return FaceSearchResult(hits=tuple(hits), model_id="rekognition:7.0")


async def test_a_different_user_ref_above_threshold_is_a_collision() -> None:
    me, them = UserRef(uuid4()), UserRef(uuid4())
    searcher = FakeSearcher(result(hit(them, 98.4)))
    cfg = make_config(enrolment_collision_threshold=97.0)

    collision = await collision_check(searcher, cfg, me, b"frame")

    assert collision is not None
    assert collision.matched_user_ref == them
    assert collision.similarity == 98.4
    assert collision.model_id == "rekognition:7.0"
    assert collision.threshold_used == 97.0


async def test_my_own_face_is_not_a_collision() -> None:
    """Re-enrolment (new phone, fresh scan) must keep working."""
    me = UserRef(uuid4())
    searcher = FakeSearcher(result(hit(me, 99.9)))

    assert await collision_check(searcher, make_config(), me, b"frame") is None


async def test_the_best_foreign_hit_wins_and_own_hits_are_skipped() -> None:
    me, a, b = UserRef(uuid4()), UserRef(uuid4()), UserRef(uuid4())
    searcher = FakeSearcher(result(hit(me, 99.9), hit(a, 97.2), hit(b, 98.8)))

    collision = await collision_check(searcher, make_config(), me, b"frame")

    assert collision is not None and collision.matched_user_ref == b


async def test_a_non_uuid_external_image_id_is_discarded() -> None:
    """We set every ExternalImageId ourselves (INVARIANTS #6); anything else is
    something we did not put there — ignore it, do not refuse a person over it."""
    me = UserRef(uuid4())
    searcher = FakeSearcher(result(hit("not-a-uuid", 99.0)))

    assert await collision_check(searcher, make_config(), me, b"frame") is None


async def test_no_hits_is_no_collision() -> None:
    assert await collision_check(FakeSearcher(result()), make_config(), UserRef(uuid4()), b"x") is None


async def test_the_search_is_parameterised_from_config_and_the_collision_key() -> None:
    """One threshold per purpose (#1b): the gate reads ITS key, not
    face_match_threshold, and asks the identity collection for one page."""
    searcher = FakeSearcher(result())
    cfg = make_config(
        enrolment_collision_threshold=97.0,
        face_match_threshold=95.0,
        enrolment_collision_max_faces=5,
    )

    await collision_check(searcher, cfg, UserRef(uuid4()), b"frame")

    assert searcher.calls == [
        {
            "collection_id": cfg.identity_collection,
            "image_bytes": b"frame",
            "threshold": 97.0,
            "max_faces": 5,
        }
    ]


async def test_a_search_failure_propagates_so_the_route_fails_closed() -> None:
    searcher = FakeSearcher(FaceIndexUnavailable("SearchFacesByImage failed"))
    with pytest.raises(FaceIndexUnavailable):
        await collision_check(searcher, make_config(), UserRef(uuid4()), b"frame")
```

(`tests/conftest.py` already exports `make_config`; if it is defined in `tests/test_config.py` instead, import from there.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_collision.py -q`
Expected: FAIL at import — `imageshield.enrolment.collision` does not exist.

- [ ] **Step 3: Add the models**

Append to `src/imageshield/enrolment/models.py`:

```python
# Stored in liveness_sessions.failure_reason when the collision gate refused
# the frame (spec 2026-09-22). Like QUALITY_REJECTED_REASON the session is
# consumed and no enrolment exists; unlike it, the replay is a 409, not a 200.
IDENTITY_CONFLICT_REASON = "identity_conflict"


@dataclass(frozen=True, slots=True)
class FaceHit:
    """One SearchFacesByImage match, raw. ``external_image_id`` is whatever the
    collection holds — parsed into a UserRef by the collision module, never
    trusted here."""

    external_image_id: str
    similarity: float
    face_id: str


@dataclass(frozen=True, slots=True)
class FaceSearchResult:
    hits: tuple[FaceHit, ...]
    model_id: str


@dataclass(frozen=True, slots=True)
class Collision:
    """The frame already belongs to somebody else. This is a REFUSAL, carried
    to the store for provenance — it is never an identity for the session."""

    matched_user_ref: UserRef
    similarity: float
    model_id: str
    threshold_used: float


@dataclass(frozen=True, slots=True)
class EnrolmentConflictRow:
    conflict_id: UUID
    session_id: UUID
    attempted_user_ref: UUID
    matched_user_ref: UUID
    similarity: float
    threshold_used: float
    model_id: str
    collection_id: str
    occurred_at: datetime
```

- [ ] **Step 4: Create the collision module**

Create `src/imageshield/enrolment/collision.py`:

```python
"""The enrolment collision gate — the ONE face search in the enrolment path.

INVARIANTS #1, as reworded 2026-09-22: a search here may REFUSE an enrolment
and may never ASSIGN, mint, merge or overwrite a ``user_ref``. This module is
built so that it cannot: it imports no store, no IndexFaces, nothing that
carries a session or an enrolment, and its only output is "this frame already
belongs to a different user_ref" or None. ``tests/test_boundaries.py``
asserts those imports structurally.

Why it exists: an on-device household member's liveness runs on the OWNER's
phone. Whoever is in front of the camera is indexed as the member — nothing
asked whether that face already had an identity. The old fear (a score
choosing who somebody is) is real and stays forbidden; a score refusing to
duplicate an identity that already exists is the opposite failure's cure.
"""

from __future__ import annotations

from typing import Protocol

import structlog

from imageshield.config import Config
from imageshield.enrolment.models import Collision, FaceSearchResult
from imageshield.types import UserRef, parse_user_ref

log = structlog.get_logger("imageshield.enrolment.collision")


class FaceSearcher(Protocol):
    """The slice of the face index this module is allowed to see: search, and
    only search. RekognitionFaceIndex satisfies it structurally; nothing here
    can reach index_face because nothing here names it."""

    async def search_face(
        self, *, collection_id: str, image_bytes: bytes, threshold: float, max_faces: int
    ) -> FaceSearchResult: ...


async def collision_check(
    searcher: FaceSearcher, cfg: Config, own_ref: UserRef, image_bytes: bytes
) -> Collision | None:
    """Return the strongest match belonging to somebody ELSE, or None.

    - ``own_ref`` hits are ignored: re-enrolment must keep working.
    - An ExternalImageId that does not parse as a UserRef is discarded: we set
      every one ourselves (INVARIANTS #6), so a stray value is something we did
      not put there — a reason to ignore that hit, not to refuse a person.
    - A provider failure propagates (FaceIndexUnavailable): the route fails
      CLOSED with 503 rather than skipping the check.
    """
    found = await searcher.search_face(
        collection_id=cfg.identity_collection,
        image_bytes=image_bytes,
        threshold=cfg.enrolment_collision_threshold,
        max_faces=cfg.enrolment_collision_max_faces,
    )
    best: tuple[float, UserRef] | None = None
    for hit in found.hits:
        try:
            ref = parse_user_ref(hit.external_image_id)
        except ValueError:
            continue
        if ref == own_ref:
            continue
        if best is None or hit.similarity > best[0]:
            best = (hit.similarity, ref)
    if best is None:
        return None
    similarity, matched = best
    log.info(
        "enrolment.collision_detected",
        similarity=similarity,
        threshold=cfg.enrolment_collision_threshold,
        model_id=found.model_id,
    )
    return Collision(
        matched_user_ref=matched,
        similarity=similarity,
        model_id=found.model_id,
        threshold_used=cfg.enrolment_collision_threshold,
    )
```

(Do NOT log either `user_ref` here — the matched one is what the response must never carry, and a log line is a response to whoever reads logs.)

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_collision.py -q`
Expected: 7 passed. Then `ruff format src/imageshield/enrolment/collision.py tests/test_collision.py` and `ruff check` both files.

- [ ] **Step 6: Commit**

```bash
git add src/imageshield/enrolment/models.py src/imageshield/enrolment/collision.py tests/test_collision.py
git commit -m "enrolment: the collision module — a face search that can refuse, never assign

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 3: `RekognitionFaceIndex.search_face`

**Files:**
- Modify: `src/imageshield/enrolment/faceindex.py` (protocol at `:38-48`, implementation after `index_face`)
- Test: `tests/test_collision.py` (append)

**Interfaces:**
- Consumes: `FaceHit`, `FaceSearchResult`, `FaceIndexUnavailable` (Task 2 / existing).
- Produces: `FaceIndex.search_face(...)` on the protocol; `RekognitionFaceIndex.search_face(...)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_collision.py`:

```python
from botocore.exceptions import ClientError

from imageshield.enrolment.faceindex import RekognitionFaceIndex


class FakeRekognitionSearch:
    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self.response = response
        self.kwargs: list[dict[str, Any]] = []

    def search_faces_by_image(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


async def test_search_face_maps_the_rekognition_response() -> None:
    fake = FakeRekognitionSearch(
        {
            "FaceMatches": [
                {"Similarity": 98.4, "Face": {"FaceId": "fid-1", "ExternalImageId": "abc"}},
            ],
            "FaceModelVersion": "7.0",
        }
    )
    index = RekognitionFaceIndex(region="us-east-1", client=fake)

    found = await index.search_face(
        collection_id="identity-v1", image_bytes=b"frame", threshold=97.0, max_faces=5
    )

    assert found == FaceSearchResult(
        hits=(FaceHit(external_image_id="abc", similarity=98.4, face_id="fid-1"),),
        model_id="rekognition:7.0",
    )
    assert fake.kwargs == [
        {
            "CollectionId": "identity-v1",
            "Image": {"Bytes": b"frame"},
            "FaceMatchThreshold": 97.0,
            "MaxFaces": 5,
            # NONE on the SEARCH, deliberately: HIGH belongs to IndexFaces. A
            # frame that would fail HIGH must still be checked, not skipped past.
            "QualityFilter": "NONE",
        }
    ]


async def test_search_face_with_no_face_in_frame_is_an_empty_result_not_an_error() -> None:
    """Rekognition raises InvalidParameterException when it finds no face to
    search with. That is not an outage — IndexFaces will reject the same frame
    with a clean quality_rejected — so it must not fail the call closed."""
    fake = FakeRekognitionSearch(
        ClientError({"Error": {"Code": "InvalidParameterException", "Message": "no face"}}, "SearchFacesByImage")
    )
    index = RekognitionFaceIndex(region="us-east-1", client=fake)

    found = await index.search_face(
        collection_id="identity-v1", image_bytes=b"frame", threshold=97.0, max_faces=5
    )

    assert found.hits == ()


async def test_search_face_provider_failure_is_face_index_unavailable() -> None:
    fake = FakeRekognitionSearch(
        ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "SearchFacesByImage")
    )
    index = RekognitionFaceIndex(region="us-east-1", client=fake)
    with pytest.raises(FaceIndexUnavailable):
        await index.search_face(
            collection_id="identity-v1", image_bytes=b"frame", threshold=97.0, max_faces=5
        )
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_collision.py -q -k search_face`
Expected: FAIL — `RekognitionFaceIndex` has no attribute `search_face`.

- [ ] **Step 3: Implement**

In `faceindex.py`, add to the imports: `from imageshield.enrolment.models import FaceHit, FaceIndexUnavailable, FaceSearchResult, IndexedFace, IndexRejected`. Add to the `FaceIndex` protocol:

```python
    async def search_face(
        self, *, collection_id: str, image_bytes: bytes, threshold: float, max_faces: int
    ) -> FaceSearchResult: ...
```

Add to `RekognitionFaceIndex`, after `index_face`:

```python
    async def search_face(
        self, *, collection_id: str, image_bytes: bytes, threshold: float, max_faces: int
    ) -> FaceSearchResult:
        """SearchFacesByImage for the collision gate (enrolment/collision.py).

        THE ONLY CALLER IS THE COLLISION MODULE, which can refuse an enrolment
        and cannot assign one. Nothing else in the enrolment path may call
        this — tests/test_boundaries.py names the exemption file by file.
        """
        try:
            response = await asyncio.to_thread(
                self._client.search_faces_by_image,
                CollectionId=collection_id,
                Image={"Bytes": image_bytes},
                FaceMatchThreshold=threshold,
                MaxFaces=max_faces,
                QualityFilter="NONE",
            )
        except ClientError as exc:
            if self._error_code(exc) == "InvalidParameterException":
                # No searchable face in the frame. Not an outage: IndexFaces
                # will answer the same frame with quality_rejected.
                return FaceSearchResult(hits=(), model_id="rekognition:unknown")
            raise self._unavailable("SearchFacesByImage", exc) from exc

        hits = tuple(
            FaceHit(
                external_image_id=str(match.get("Face", {}).get("ExternalImageId", "")),
                similarity=float(match.get("Similarity", 0.0)),
                face_id=str(match.get("Face", {}).get("FaceId", "")),
            )
            for match in response.get("FaceMatches") or []
        )
        return FaceSearchResult(
            hits=hits, model_id=f"rekognition:{response.get('FaceModelVersion', 'unknown')}"
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_collision.py -q`
Expected: 10 passed. `ruff check src/imageshield/enrolment/faceindex.py`.

Note: `tests/test_boundaries.py::test_face_search_appears_only_in_the_attribution_module` and `::test_no_face_search_in_the_enrolment_path` will now FAIL (faceindex.py and collision.py contain `search_faces_by_image`). That is expected and is fixed in Task 7, which must land before the suite is green. Commit anyway — the boundary tests are the point of Task 7.

- [ ] **Step 5: Commit**

```bash
git add src/imageshield/enrolment/faceindex.py tests/test_collision.py
git commit -m "faceindex: search_face for the collision gate — QualityFilter NONE, no-face is empty not an outage

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 4: Migration 0036 — `enrolment_conflicts`

**Files:**
- Create: `migrations/0036_enrolment_conflicts.up.sql`, `migrations/0036_enrolment_conflicts.down.sql`
- Test: `tests/test_enrolment_constraints.py` (append)

**Interfaces:**
- Produces: table `enrolment_conflicts` as in the spec §4; `SELECT, INSERT` to `identity_rw`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_enrolment_constraints.py` (use the file's existing `migrated_db` / connection fixture — read its first 40 lines and mirror the fixture names exactly):

```python
async def test_enrolment_conflicts_is_one_row_per_session(migrated_db: str) -> None:
    """A session conflicts at most once: the UNIQUE on session_id is the
    provenance rule, and the FK means a conflict can never outlive its session."""
    pool = make_async_pool(migrated_db, min_size=1, max_size=1)
    await pool.open()
    try:
        async with pool.connection() as conn:
            sid = uuid4()
            await conn.execute(
                "INSERT INTO liveness_sessions (session_id, user_ref, provider_session_id, status, expires_at)"
                " VALUES (%s, %s, 'prov-1', 'created', now() + interval '10 minutes')",
                (sid, uuid4()),
            )
            insert = (
                "INSERT INTO enrolment_conflicts (session_id, attempted_user_ref, matched_user_ref,"
                " similarity, threshold_used, model_id, collection_id)"
                " VALUES (%s, %s, %s, 98.4, 97.0, 'rekognition:7.0', 'identity-v1')"
            )
            await conn.execute(insert, (sid, uuid4(), uuid4()))
            with pytest.raises(psycopg.errors.UniqueViolation):
                await conn.execute(insert, (sid, uuid4(), uuid4()))
    finally:
        await pool.close()
```

(Match the `liveness_sessions` INSERT to the columns the file's other tests use — copy their insert verbatim if one exists.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_enrolment_constraints.py -q -k conflicts`
Expected: FAIL — relation `enrolment_conflicts` does not exist.

- [ ] **Step 3: Write the migration pair**

`migrations/0036_enrolment_conflicts.up.sql`:

```sql
-- 0036 — enrolment_conflicts: the provenance row behind 409 identity_conflict.
--
-- The collision gate (enrolment/collision.py, spec 2026-09-22) refuses to
-- index a liveness frame that already belongs to a DIFFERENT user_ref. A
-- refusal must be traceable to the search that produced it — which face it
-- matched, at what similarity, against which threshold and model, in which
-- collection — for the same reason attribution_runs carries match_threshold
-- (CLAUDE.md §5: every derived row carries provenance). This is where that
-- lives. The matched user_ref is HERE and nowhere on the wire: the HTTP
-- response carries conflict_id only.
--
-- UNIQUE(session_id): a session conflicts at most once — the row is written
-- in the same transaction that consumes the session. The FK means a conflict
-- cannot outlive its session.
--
-- Grants follow 0015/0018: privileges attach to the identity_rw module role,
-- login roles are members of it. NO grant to imageshield_proxy_ro — the
-- proxy learns of a conflict through the 409 and its own audit row, never by
-- reading who was matched.

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

CREATE INDEX enrolment_conflicts_matched_idx ON enrolment_conflicts (matched_user_ref);

GRANT SELECT, INSERT ON enrolment_conflicts TO identity_rw;
```

`migrations/0036_enrolment_conflicts.down.sql`:

```sql
-- Reverse 0036. Revoke before dropping (0035's rule #1); the role itself is
-- 0015's and stays.
REVOKE ALL ON enrolment_conflicts FROM identity_rw;
DROP TABLE enrolment_conflicts;
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_enrolment_constraints.py -q`
Expected: all pass. Then the round trip: `python scripts/migrate.py up && python scripts/migrate.py down --steps 1 && python scripts/migrate.py up` against your local compose database (the runner's checksummed ledger must accept it both ways).

- [ ] **Step 5: Commit**

```bash
git add migrations/0036_enrolment_conflicts.up.sql migrations/0036_enrolment_conflicts.down.sql tests/test_enrolment_constraints.py
git commit -m "migration 0036: enrolment_conflicts — provenance for a refused enrolment

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 5: Store — `finalize_conflict` and `get_conflict`

**Files:**
- Modify: `src/imageshield/liveness/store.py` (after `finalize_quality_rejected`, ~line 395)
- Modify: `src/imageshield/http/routes/liveness.py`'s `LivenessStore` protocol if one is declared separately (check `src/imageshield/liveness/store.py` for a `class LivenessStore(Protocol)`; add both methods there too)
- Test: `tests/test_enrolment_store.py` (append)

**Interfaces:**
- Consumes: `Collision`, `EnrolmentConflictRow`, `IDENTITY_CONFLICT_REASON` (Task 2); `_CONSUME_SQL`, `_to_row` (existing).
- Produces:
  ```python
  async def finalize_conflict(self, session_id: SessionId, *, confidence: float | None, reference_image_uri: str, audit_image_uris: tuple[str, ...], collection_id: str, collision: Collision) -> tuple[LivenessSessionRow, EnrolmentConflictRow] | None
  async def get_conflict(self, session_id: SessionId) -> EnrolmentConflictRow | None
  ```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_enrolment_store.py` (reuse its `stores` fixture, which yields `(PostgresLivenessStore, PostgresEnrolmentStore)`, and its existing helper that inserts a `created` session — read the file and call that helper; below it is named `seed_session`):

```python
from imageshield.enrolment.models import Collision, IDENTITY_CONFLICT_REASON


async def test_finalize_conflict_consumes_and_records_in_one_transaction(stores) -> None:
    liveness, _ = stores
    row = await seed_session(liveness)           # a 'created' session for some user_ref
    them = UserRef(uuid4())

    outcome = await liveness.finalize_conflict(
        SessionId(row.session_id),
        confidence=99.1,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=("https://proxy-s3.example/a0.jpg",),
        collection_id="identity-v1",
        collision=Collision(matched_user_ref=them, similarity=98.4, model_id="rekognition:7.0", threshold_used=97.0),
    )

    assert outcome is not None
    session, conflict = outcome
    assert session.status == "consumed"
    assert session.failure_reason == IDENTITY_CONFLICT_REASON
    assert session.consumed_at is not None and session.completed_at is not None
    assert conflict.session_id == row.session_id
    assert conflict.attempted_user_ref == row.user_ref
    assert conflict.matched_user_ref == them
    assert float(conflict.similarity) == 98.4
    assert float(conflict.threshold_used) == 97.0
    # No enrolment, no subject: a refused frame creates nobody.
    assert await liveness.get_enrolment_consent_ref(SessionId(row.session_id)) is None
    assert await liveness.get_conflict(SessionId(row.session_id)) == conflict


async def test_finalize_conflict_loses_cleanly_to_a_concurrent_finalizer(stores) -> None:
    liveness, _ = stores
    row = await seed_session(liveness)
    await liveness.finalize_quality_rejected(
        SessionId(row.session_id), confidence=99.0,
        reference_image_uri="https://x/ref.jpg", audit_image_uris=(),
    )

    outcome = await liveness.finalize_conflict(
        SessionId(row.session_id), confidence=99.0,
        reference_image_uri="https://x/ref.jpg", audit_image_uris=(),
        collection_id="identity-v1",
        collision=Collision(matched_user_ref=UserRef(uuid4()), similarity=98.0, model_id="m", threshold_used=97.0),
    )

    assert outcome is None
    assert await liveness.get_conflict(SessionId(row.session_id)) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_enrolment_store.py -q -k conflict`
Expected: FAIL — `PostgresLivenessStore` has no attribute `finalize_conflict`.

- [ ] **Step 3: Implement**

In `store.py`, add near the other SQL constants:

```python
_CONFLICT_COLUMNS = (
    "conflict_id, session_id, attempted_user_ref, matched_user_ref, similarity,"
    " threshold_used, model_id, collection_id, occurred_at"
)

_INSERT_CONFLICT_SQL = f"""
    INSERT INTO enrolment_conflicts
      (session_id, attempted_user_ref, matched_user_ref, similarity, threshold_used,
       model_id, collection_id)
    VALUES
      (%(session_id)s, %(attempted_user_ref)s, %(matched_user_ref)s, %(similarity)s,
       %(threshold_used)s, %(model_id)s, %(collection_id)s)
    RETURNING {_CONFLICT_COLUMNS}
"""

_SELECT_CONFLICT_SQL = f"""
    SELECT {_CONFLICT_COLUMNS} FROM enrolment_conflicts WHERE session_id = %(session_id)s
"""

# actor_type 'service' as in subjects/store.py: the proxy acting for a user.
# subject_ref is the ATTEMPTED user_ref; the matched one is on the conflicts
# table, for staff with a reason, and not in an audit trail read more widely.
_AUDIT_CONFLICT_SQL = """
    INSERT INTO audit_log (actor_type, action, subject_ref, resource_id, metadata)
    VALUES ('service', 'enrolment.identity_conflict', %(subject_ref)s, %(resource_id)s,
            %(metadata)s)
"""


def _to_conflict_row(record: tuple[Any, ...]) -> EnrolmentConflictRow:
    (conflict_id, session_id, attempted, matched, similarity, threshold, model_id,
     collection_id, occurred_at) = record
    return EnrolmentConflictRow(
        conflict_id=conflict_id, session_id=session_id, attempted_user_ref=attempted,
        matched_user_ref=matched, similarity=float(similarity), threshold_used=float(threshold),
        model_id=model_id, collection_id=collection_id, occurred_at=occurred_at,
    )
```

Add to `PostgresLivenessStore` after `finalize_quality_rejected`:

```python
    async def finalize_conflict(
        self,
        session_id: SessionId,
        *,
        confidence: float | None,
        reference_image_uri: str,
        audit_image_uris: tuple[str, ...],
        collection_id: str,
        collision: Collision,
    ) -> tuple[LivenessSessionRow, EnrolmentConflictRow] | None:
        """Consume the session, record WHY, audit it — one transaction.

        Mirrors finalize_quality_rejected: liveness passed, enrolment did not,
        the session is spent so the person can start the fresh one that is the
        remedy. Unlike it, provenance is written: the search that refused this
        frame is on enrolment_conflicts with its threshold and model. NO
        enrolment, NO subject row, NO IndexFaces happened.
        """
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _CONSUME_SQL,
                {
                    "session_id": session_id,
                    "confidence": confidence,
                    "failure_reason": IDENTITY_CONFLICT_REASON,
                    "reference_image_uri": reference_image_uri,
                    "audit_image_uris": list(audit_image_uris),
                },
            )
            session_record = await cur.fetchone()
            if session_record is None:
                return None  # concurrent finalizer won; caller compensates
            session = _to_row(session_record)
            cur = await conn.execute(
                _INSERT_CONFLICT_SQL,
                {
                    "session_id": session_id,
                    "attempted_user_ref": session.user_ref,
                    "matched_user_ref": collision.matched_user_ref,
                    "similarity": collision.similarity,
                    "threshold_used": collision.threshold_used,
                    "model_id": collision.model_id,
                    "collection_id": collection_id,
                },
            )
            conflict_record = await cur.fetchone()
            assert conflict_record is not None
            conflict = _to_conflict_row(conflict_record)
            await conn.execute(
                _AUDIT_CONFLICT_SQL,
                {
                    "subject_ref": session.user_ref,
                    "resource_id": conflict.conflict_id,
                    "metadata": Jsonb(
                        {"similarity": collision.similarity, "threshold": collision.threshold_used}
                    ),
                },
            )
        return session, conflict

    async def get_conflict(self, session_id: SessionId) -> EnrolmentConflictRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_SELECT_CONFLICT_SQL, {"session_id": session_id})
            record = await cur.fetchone()
        return _to_conflict_row(record) if record is not None else None
```

Imports: `from psycopg.types.json import Jsonb` (check how `subjects/store.py` passes `metadata` and copy that exact form), `from imageshield.enrolment.models import Collision, EnrolmentConflictRow, IDENTITY_CONFLICT_REASON, ...`. If `LivenessStore` is a Protocol in this file, add both method signatures to it.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_enrolment_store.py tests/test_liveness_store.py -q`
Expected: all pass. `ruff check src/imageshield/liveness/store.py`.

- [ ] **Step 5: Commit**

```bash
git add src/imageshield/liveness/store.py tests/test_enrolment_store.py
git commit -m "liveness store: finalize_conflict — consume, record provenance, audit, in one transaction

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 6: The route — gate before `IndexFaces`, 409 with `conflict_id`, replay stays a 409

**Files:**
- Modify: `src/imageshield/http/errors.py:36-42, 73-76`
- Modify: `src/imageshield/http/routes/liveness.py` (docstring `:1-12`; `_result_response` `:131`; the succeeded path before `face_index.index_face` `~:383`; `_completed_replay` `:466`)
- Test: `tests/test_liveness_routes.py` (`FakeFaceIndex` `:286-316`; append tests)

**Interfaces:**
- Consumes: `collision_check`, `Collision`, `IDENTITY_CONFLICT_REASON`, `finalize_conflict`, `get_conflict`.
- Produces: `ServiceError(status, code, message, *, retryable, extra=None)`; HTTP `409 identity_conflict` with `error.conflict_id`.

- [ ] **Step 1: Extend the fake and write the failing tests**

In `tests/test_liveness_routes.py`, extend `FakeFaceIndex`:

```python
    # Collision gate (spec 2026-09-22): what search_face answers. Default: a
    # collection that matches nothing, so every existing test still enrols.
    search_result: FaceSearchResult | Exception = FaceSearchResult(hits=(), model_id="rekognition:7.0")
    search_calls: list[dict[str, Any]]  # set in __init__: self.search_calls = []

    async def search_face(
        self, *, collection_id: str, image_bytes: bytes, threshold: float, max_faces: int
    ) -> FaceSearchResult:
        self.search_calls.append(
            {"collection_id": collection_id, "image_bytes": image_bytes,
             "threshold": threshold, "max_faces": max_faces}
        )
        if isinstance(self.search_result, Exception):
            raise self.search_result
        return self.search_result
```

Also rewrite its class docstring (it currently says "nothing ever searches" — that is no longer true; say the search can refuse and cannot assign). Extend `FakeLivenessStore` with `finalize_conflict` (set `status='consumed'`, `failure_reason=IDENTITY_CONFLICT_REASON`, store a `EnrolmentConflictRow` in `self.conflicts[session_id]`, return `(row, conflict)`, or `None` if `completed_at` already set) and `get_conflict`.

Append tests:

```python
def conflict_body(response: Any) -> dict[str, Any]:
    body = response.json()
    assert set(body) == {"error"}
    envelope: dict[str, Any] = body["error"]
    assert set(envelope) == {"code", "message", "retryable", "request_id", "conflict_id"}
    return envelope


def test_result_refuses_a_frame_already_enrolled_to_someone_else() -> None:
    """THE GATE. Owner scans their own face as the member: 409, nothing indexed,
    no enrolment, no subject, session consumed, one conflict row — and the
    matched person is nowhere in the response."""
    h = Harness(enrolment_collision_threshold=97.0)
    owner = uuid4()
    h.face_index.search_result = FaceSearchResult(
        hits=(FaceHit(external_image_id=str(owner), similarity=98.4, face_id="f-owner"),),
        model_id="rekognition:7.0",
    )
    row = h.store.add(make_row())  # a DIFFERENT user_ref: the member
    h.passed_provider_result(row.provider_session_id)

    response = h.result(row.session_id)

    assert response.status_code == 409
    envelope = conflict_body(response)
    assert envelope["code"] == "identity_conflict"
    assert envelope["retryable"] is False
    UUID(envelope["conflict_id"])
    assert str(owner) not in response.text
    assert h.face_index.index_calls == []            # nothing indexed
    assert h.store.enrolments == {}                  # no enrolment
    assert h.store.subjects == {}                    # no subject
    stored = h.store.rows[row.session_id]
    assert stored.status == "consumed"
    assert stored.failure_reason == "identity_conflict"
    assert row.session_id in h.store.conflicts


def test_result_indexes_when_the_only_match_is_the_same_person() -> None:
    """Re-enrolment must keep working."""
    h = Harness()
    row = h.store.add(make_row())
    h.face_index.search_result = FaceSearchResult(
        hits=(FaceHit(external_image_id=str(row.user_ref), similarity=99.7, face_id="f-me"),),
        model_id="rekognition:7.0",
    )
    h.passed_provider_result(row.provider_session_id)

    response = h.result(row.session_id)

    assert response.status_code == 200 and response.json()["enrolled"] is True
    assert len(h.face_index.index_calls) == 1


def test_result_gate_runs_before_index_faces_and_reads_the_collision_threshold() -> None:
    h = Harness(enrolment_collision_threshold=97.0, face_match_threshold=95.0)
    row = h.store.add(make_row())
    h.passed_provider_result(row.provider_session_id)

    h.result(row.session_id)

    assert h.face_index.search_calls == [
        {"collection_id": "identity-v1", "image_bytes": b"reference-jpeg-bytes",
         "threshold": 97.0, "max_faces": 5}
    ]


def test_result_search_failure_fails_closed_with_503_and_writes_nothing() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.face_index.search_result = FaceIndexUnavailable("SearchFacesByImage failed")
    h.passed_provider_result(row.provider_session_id)

    response = h.result(row.session_id)

    assert response.status_code == 503
    assert error_body(response)["code"] == "face_index_unavailable"
    assert h.face_index.index_calls == []
    assert h.store.rows[row.session_id].completed_at is None   # not consumed: retry works


def test_result_replay_of_a_conflict_is_the_same_409_never_a_200() -> None:
    h = Harness()
    h.face_index.search_result = FaceSearchResult(
        hits=(FaceHit(external_image_id=str(uuid4()), similarity=99.0, face_id="f"),),
        model_id="rekognition:7.0",
    )
    row = h.store.add(make_row())
    h.passed_provider_result(row.provider_session_id)
    first = h.result(row.session_id, key="k1")
    assert first.status_code == 409

    replay = h.result(row.session_id, key="k1")

    assert replay.status_code == 409
    assert conflict_body(replay)["conflict_id"] == conflict_body(first)["conflict_id"]
    assert len(h.provider.get_calls) == 1     # replay did not hit the provider again
```

Ensure imports at the top of the test file include `FaceHit`, `FaceSearchResult`, `FaceIndexUnavailable`, `IDENTITY_CONFLICT_REASON`, `EnrolmentConflictRow`, and `UUID`.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_liveness_routes.py -q -k "conflict or gate or fails_closed or same_person"`
Expected: the gate test gets `200` (enrolled) instead of 409; fails-closed test gets 200; replay test fails.

- [ ] **Step 3: `ServiceError` carries `extra`**

In `errors.py`:

```python
class ServiceError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        # Additional envelope fields (e.g. conflict_id). Never a user_ref, a
        # phone, or anything the request sent us — the envelope is what the
        # proxy logs.
        self.extra: dict[str, Any] = dict(extra or {})
```

and in the handler: `return _envelope(exc.status_code, exc.code, exc.message, retryable=exc.retryable, **exc.extra)`. Add `from collections.abc import Mapping` and `from typing import Any` if missing.

- [ ] **Step 4: Wire the route**

In `liveness.py`:

Imports: add `from imageshield.enrolment.collision import collision_check` and `IDENTITY_CONFLICT_REASON`, `EnrolmentConflictRow` to the models import.

Replace the module docstring's first bullet (lines 5–8) with:

```
- Identity is the ``user_ref`` in the request. The ONE face search in this
  path — ``enrolment/collision.py``, run before IndexFaces — may REFUSE to
  index a frame that already belongs to a different user_ref (409
  identity_conflict). It can never assign one: INVARIANTS.md #1 as reworded
  2026-09-22. The old fragmentation bug (a score choosing who you are) stays
  forbidden; this is its opposite failure's cure.
```

Add helpers next to `_result_response`:

```python
def _conflict_error(conflict: EnrolmentConflictRow) -> ServiceError:
    # conflict_id ONLY. The matched person is on enrolment_conflicts for staff
    # with a reason and never on the wire — the proxy's own audit promises it.
    return ServiceError(
        409,
        "identity_conflict",
        "This face is already enrolled to a different person. Nothing was"
        " indexed; start a fresh session with the right person and quote the"
        " conflict_id to support.",
        retryable=False,
        extra={"conflict_id": str(conflict.conflict_id)},
    )


async def _replay(store: LivenessStore, row: LivenessSessionRow) -> LivenessResultResponse:
    """A same-key replay reproduces the ORIGINAL answer. A conflicted session
    replays its 409 — never a 200 that would read as 'passed, not enrolled'."""
    if row.failure_reason == IDENTITY_CONFLICT_REASON:
        conflict = await store.get_conflict(SessionId(row.session_id))
        if conflict is not None:
            raise _conflict_error(conflict)
    return _result_response(row)
```

In `post_liveness_result`, change the two replay sites: the `if row.completed_at is not None:` branch's `return _result_response(row)` → `return await _replay(store, row)`; and in `_completed_replay` → `return await _replay(store, row)`.

Insert the gate immediately before `# Liveness passed and the frames are persisted. Index the ReferenceImage`:

```python
    # THE COLLISION GATE (spec 2026-09-22), BEFORE IndexFaces: a frame that
    # already belongs to a different user_ref is refused and nothing is
    # indexed, so a conflict leaves the collection untouched — the same
    # guarantee quality_rejected gives. Same bytes IndexFaces is about to use.
    try:
        collision = await collision_check(
            face_index, cfg, UserRef(row.user_ref), result.reference_image
        )
    except FaceIndexUnavailable:
        # FAIL CLOSED. An unavailable check must not become a skipped check.
        # Nothing written, nothing consumed: same-key retry re-runs from the top.
        raise ServiceError(
            503,
            "face_index_unavailable",
            "Face indexing is temporarily unavailable; retry with the same"
            " Idempotency-Key.",
            retryable=True,
        ) from None
    if collision is not None:
        conflicted = await store.finalize_conflict(
            sid,
            confidence=result.confidence,
            reference_image_uri=_strip_query(body.reference_put_url),
            audit_image_uris=tuple(stored_audit_uris),
            collection_id=cfg.identity_collection,
            collision=collision,
        )
        if conflicted is None:
            return await _completed_replay(store, sid, idempotency_key)
        _, conflict = conflicted
        log.info(
            "liveness.enrolment_identity_conflict",
            session_id=str(sid),
            conflict_id=str(conflict.conflict_id),
            similarity=collision.similarity,
        )
        raise _conflict_error(conflict)
```

Also update `_enrolled()`'s comment: "only the enrolment, quality-rejected and identity-conflict paths consume, and the latter two set failure_reason."

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_liveness_routes.py tests/test_enrolment_routes.py -q`
Expected: all pass, including every pre-existing test (the fake's default search result matches nothing). `ruff check` both source files.

- [ ] **Step 6: Commit**

```bash
git add src/imageshield/http/errors.py src/imageshield/http/routes/liveness.py tests/test_liveness_routes.py
git commit -m "liveness: the collision gate before IndexFaces — 409 identity_conflict, fail closed, replay stays 409

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 7: Boundary tests — the exemption, and proof the module cannot write

**Files:**
- Modify: `tests/test_boundaries.py:14-60, 189-240`

**Interfaces:** none (tests only).

- [ ] **Step 1: Run the suite to see the two expected failures**

Run: `.venv/Scripts/python -m pytest tests/test_boundaries.py -q`
Expected: `test_no_face_search_in_the_enrolment_path` and `test_face_search_appears_only_in_the_attribution_module` FAIL, naming `enrolment/collision.py` and `enrolment/faceindex.py`.

- [ ] **Step 2: Add the exemption and the two structural tests**

After `ATTRIBUTION_DIR = "attribution"` add:

```python
# The SECOND exemption (INVARIANTS #1 as reworded 2026-09-22). The collision
# gate searches the identity collection with the liveness frame BEFORE
# IndexFaces and may REFUSE the enrolment; it can never assign one, and
# test_the_collision_module_cannot_assign is what makes that structural rather
# than promised. Two files, named individually: the module that decides, and
# the Rekognition wrapper that carries the call. Adding a third is a code
# change and a review — the same rule as ATTRIBUTION_DIR.
COLLISION_FILES = (
    "enrolment/collision.py",
    "enrolment/faceindex.py",
)

# What the collision module may NOT touch. A module that cannot reach a store
# or IndexFaces cannot mint, overwrite or merge a user_ref, whatever its search
# returns. The grep IS the proof.
COLLISION_MUST_NOT_REFERENCE = re.compile(
    r"liveness\.store|enrolment\.store|subjects\.|index_face|IndexFaces|INSERT|UPDATE|"
    r"psycopg|_pool|conn\."
)
```

Change `_enrolment_path_files()` to skip `COLLISION_FILES` (relative to `SRC / "imageshield"`), and `_files_outside_attribution()` to skip them too. Update the docstring of `test_no_face_search_in_the_enrolment_path` with one sentence: "The collision gate is exempt file by file (COLLISION_FILES) and separately proven unable to write."

Append:

```python
def test_the_collision_module_cannot_assign() -> None:
    """PERMANENT. INVARIANTS #1, reworded: a search may refuse, never assign.

    The exemption above is safe only while the module holding the search has
    no way to write. If someone gives collision.py a store, this fails before
    the first enrolment is minted from a similarity score.
    """
    module = SRC / "imageshield" / "enrolment" / "collision.py"
    text = module.read_text(encoding="utf-8")
    offenders = sorted({m.group(0) for m in COLLISION_MUST_NOT_REFERENCE.finditer(text)})
    assert offenders == [], f"collision.py reaches a write path: {offenders}"


def test_the_collision_exemption_is_actually_used() -> None:
    """PERMANENT. An exemption nobody uses should be DELETED, not widened."""
    module = SRC / "imageshield" / "enrolment" / "collision.py"
    assert module.is_file(), "collision.py is gone — remove COLLISION_FILES"
    wrapper = SRC / "imageshield" / "enrolment" / "faceindex.py"
    assert FORBIDDEN_SEARCH.search(wrapper.read_text(encoding="utf-8")), (
        "faceindex.py no longer calls face search — delete the exemption"
    )
    assert "search_face" in module.read_text(encoding="utf-8")
```

- [ ] **Step 3: Run to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_boundaries.py -q`
Expected: all pass. Deliberately break it once: add `from imageshield.liveness.store import x` to collision.py, run, see `test_the_collision_module_cannot_assign` fail, revert.

- [ ] **Step 4: Commit**

```bash
git add tests/test_boundaries.py
git commit -m "boundaries: the collision gate is exempt file by file and proven unable to write

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 8: Docs — the reworded invariant and the new 409

**Files:**
- Modify: `INVARIANTS.md:12-30`, `CLAUDE.md:194-198`, `PROXY_INTEGRATION.md:207` (the result-endpoint row) and the error table near `:315`
- Modify (backend repo, separate commit): `image_backend/docs/CLAUDE.md:473` (P5 phase-7 status)

- [ ] **Step 1: INVARIANTS.md #1** — after the "Check:" line (line 29) add:

```markdown
**Reworded 2026-09-22 (spec `docs/superpowers/specs/2026-09-22-enrolment-collision-gate-design.md`):**
a face search in the enrolment path may **refuse** an enrolment; it may never **assign, mint, merge or
overwrite** a `user_ref`. The one such search lives in `enrolment/collision.py`: it runs before
`IndexFaces`, returns only "this face already belongs to a different `user_ref`", and that module
cannot write — `tests/test_boundaries.py::test_the_collision_module_cannot_assign` is the proof, and
the enrolment-path grep exempts exactly two named files. Why it was added: an on-device member's
liveness runs on the owner's phone, and without this the owner's own face could be indexed as the
member — two identities sharing one vector, with attribution handing the owner's photos and hits to
the member. The old rule was right that a score must not CHOOSE an identity and wrong that it may not
REFUSE to duplicate one.
```

- [ ] **Step 2: CLAUDE.md §4 #1** — append to the item at line 194: *"**Amended 2026-09-22:** the one exception is `enrolment/collision.py`, which may REFUSE an enrolment whose frame already matches a different `user_ref` (`409 identity_conflict`) and structurally cannot assign one. `SearchFacesByImage` still may not appear anywhere else in the enrolment path."* Also delete the now-false sentence in §2 or wherever `config.py`'s "no collision check" reasoning is echoed, if any (`grep -n "collision" CLAUDE.md`).

- [ ] **Step 3: PROXY_INTEGRATION.md** — in the `POST /v1/liveness/{sid}/result` row add: *"`409 identity_conflict` (2026-09-22) — the frame matched a different `user_ref` at or above `ENROLMENT_COLLISION_THRESHOLD`; nothing indexed, no subject, session consumed; envelope carries `conflict_id` (a support handle) and never the matched person. Terminal: start a fresh session with the right person. A same-key replay returns the same 409."* Add a row to the terminal-errors table near line 315 with the same text.

- [ ] **Step 4: Backend `docs/CLAUDE.md` P5 phase-7 status (separate repo, separate commit)** — replace the paragraph at line 473 ("the collision gate … is **not built** …") with: *"**BUILT on the services side 2026-09-22** (their spec `2026-09-22-enrolment-collision-gate-design.md`): `POST /v1/liveness/{sid}/result` answers `409 identity_conflict` before `IndexFaces` when the frame matches a different `user_ref`; our `IDENTITY_CONFLICT` mapping and audit row now fire. Their INVARIANTS #1 was reworded to 'may refuse, never assign' rather than reversed."*

- [ ] **Step 5: Commit (services), then commit (backend)**

```bash
# services
git add INVARIANTS.md CLAUDE.md PROXY_INTEGRATION.md
git commit -m "docs: INVARIANTS #1 reworded — a search may refuse an enrolment, never assign one

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
# backend (release/sep-1)
cd ../image_backend && git add docs/CLAUDE.md
git commit -m "docs: P5 phase-7 status — the services collision gate is built

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 9 (optional, backend): lift `conflict_id` into the audit row

**Files:**
- Modify: `image_backend/src/enrolment/gateway.ts:141-160, 251` (the failure-body reader)
- Test: `image_backend/tests/unit/enrolment-outcomes.test.ts` or the gateway test that exists (find with `grep -rln "IDENTITY_CONFLICT" tests/`)

- [ ] **Step 1: Failing test** — a 409 body `{ error: { code: 'identity_conflict', conflict_id: 'abc…' } }` maps to an `AppError` whose `details.conflict_id === 'abc…'`.
- [ ] **Step 2: Implement** — where the gateway lifts `error.code`, also lift `error.conflict_id` (a string, validated as UUID; anything else dropped) and pass it through `mapServicesError` into `details`. Keep the existing comment's spirit: still nothing else from the body survives.
- [ ] **Step 3: Verify** the `enrolment.conflict` audit row's `conflict_id` metadata is populated in the existing service test; run `npx vitest run tests/unit tests/integration/enrolment*.test.ts`.
- [ ] **Step 4: Commit** on `release/sep-1` with the standard trailer.

---

## Self-review against the spec

- §1 rule / properties 1–3 → Tasks 2 (block-only module), 2 (own-ref ignored), 6 (fail closed). ✔
- §2 placement before `IndexFaces`, same bytes → Task 6 (gate reads `result.reference_image`; test asserts `image_bytes == b"reference-jpeg-bytes"` and `index_calls == []` on conflict). ✔
- §3 module shape, `QualityFilter=NONE`, non-UUID discarded, own dependency via protocol → Tasks 2, 3. ✔
- §4 one transaction, conflicts table, audit row without matched ref, no enrolment/subject → Tasks 4, 5. ✔
- §5 envelope `conflict_id`, never matched ref, replay stays 409 → Task 6; optional backend lift → Task 9. ✔
- §6 threshold 97 in `.env.example`; the deployed value is set in the task definitions at deploy time (not in this plan — deploy note). ✔
- §7 boundary tests three-way → Task 7; route tests seven cases → Task 6 (five) + Task 2 (below-threshold is the searcher's `FaceMatchThreshold`, covered by the parameterisation test; non-UUID covered). ✔
- §8 non-goals — none built. ✔
- Type consistency: `FaceSearchResult(hits, model_id)`, `Collision(matched_user_ref, similarity, model_id, threshold_used)`, `finalize_conflict(..., collection_id, collision) -> tuple[LivenessSessionRow, EnrolmentConflictRow] | None`, `get_conflict(session_id)` — used identically in Tasks 2, 5, 6. ✔
