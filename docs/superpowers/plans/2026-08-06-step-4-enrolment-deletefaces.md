# Step 4 — Enrolment from ReferenceImage + DeleteFaces Path — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `POST /v1/liveness/:session_id/result` so a passed liveness session indexes the in-memory ReferenceImage into Rekognition `identity-v1` and writes an `enrolments` row atomically with session consumption; build `DELETE /v1/enrolments/:user_ref` with DeleteFaces → verify → tombstone ordering.

**Architecture:** A new `imageshield.enrolment` package holds the domain models, a `FaceIndex` protocol with a boto3 Rekognition implementation (IndexFaces / DeleteFaces / ListFaces), and the delete-path store. The liveness store gains two transactional finalizers (`finalize_enrolled`, `finalize_quality_rejected`) that update `liveness_sessions` and insert `enrolments` in ONE transaction, with `NOTIFY enrolment_complete` inside it. Migration 0003 makes "no enrolment without a consumed (passed) session" a database constraint via a composite FK.

**Tech Stack:** Python 3.11, FastAPI, psycopg 3 raw SQL, boto3 Rekognition (sanctioned importer), pytest + throwaway Postgres.

## Global Constraints

- `ExternalImageId` = `str(user_ref)` and nothing else (CLAUDE.md §4 #6).
- `QualityFilter='HIGH'`, `MaxFaces=1`, `DetectionAttributes=[]` on IndexFaces — exactly.
- No `SearchFacesByImage` / `search_faces_by_image` / `search_users_by_image` anywhere in `src/`.
- No S3 client, no `bytea` column, no image bytes persisted (INVARIANTS #9).
- No inline threshold literals — everything from `Config` (INVARIANTS #1b).
- The `UNIQUE` FK on `enrolments.session_id` is the single-use enforcement — no duplicated application-level check.
- Index from the in-memory bytes already returned by `GetFaceLivenessSessionResults`. Never re-fetch.
- Transient IndexFaces failure → write nothing, do NOT consume, return 503 (proxy retries same `Idempotency-Key`).
- Quality rejection → NO enrolments row, session IS consumed, `200 {status:'passed', enrolled:false, reason:'quality_rejected'}`.
- `identity:index` queue stays provisioned but unused — indexing is synchronous. Say so in the final report; do not invent async work.
- Every inbound body is pydantic `extra='forbid'`; mypy strict + ruff must stay green.
- Line length 100 (ruff). Windows dev box; Postgres at `localhost:15433` via `docker compose -f docker-compose.local.yml up -d`.
- Stop at end of step 4. No provider work.

---

### Task 0: Carry-forward verification + baseline commit

Steps 1–3 exist in the working tree but are **uncommitted** (see `git status`). Step 4 diffs must not mix with them.

**Files:**
- Create: `devtools/check_collection.py`

- [ ] **Step 1: Commit the pending step-3 work as its own commit**

```bash
git add -A
git commit -m "Step 3: liveness session lifecycle + Rekognition integration

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 2: Verify carry-forward #1 — schema lint passes `reference_image_uri`, fails `photo bytea`**

Run: `python -m pytest tests/test_schema_lint.py -q`
Expected: PASS. Additionally confirm the two specific fixtures exist by inspection: `lint_column` on a `bytea` column returns a Violation; on `reference_image_uri text` returns None (both are covered in `tests/test_schema_lint.py`; if either specific case is missing, add it there).

- [ ] **Step 3: Write the collection-audit script**

```python
"""Spike-residue check for carry-forward #2 (step-4 brief): identity-v1 must
hold zero faces before the lookalike regression test means anything.

Devtools only — this is the sanctioned place for real-AWS spike checks
(CLAUDE.md §8). Run: python devtools/check_collection.py [--purge]
"""

from __future__ import annotations

import argparse

import boto3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", default="identity-v1")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--purge", action="store_true", help="DeleteFaces everything found")
    args = parser.parse_args()

    client = boto3.client("rekognition", region_name=args.region)
    try:
        faces: list[dict] = []
        paginator = client.get_paginator("list_faces")
        for page in paginator.paginate(CollectionId=args.collection):
            faces.extend(page["Faces"])
    except client.exceptions.ResourceNotFoundException:
        print(f"{args.collection}: collection does not exist (zero faces, trivially)")
        return

    print(f"{args.collection}: {len(faces)} faces")
    for face in faces:
        print(f"  {face['FaceId']}  ExternalImageId={face.get('ExternalImageId')}")

    if faces and args.purge:
        client.delete_faces(
            CollectionId=args.collection, FaceIds=[f["FaceId"] for f in faces]
        )
        print(f"purged {len(faces)} faces")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run it with real credentials (memory: account mokshith_dev / us-east-1 works)**

Run: `python devtools/check_collection.py`
Expected: `0 faces` or `collection does not exist`. If faces are listed, they are spike residue — the brief sanctions resolving it: re-run with `--purge`, then re-run plain to confirm zero.

- [ ] **Step 5: Commit**

```bash
git add devtools/check_collection.py
git commit -m "Step 4 pre-flight: identity-v1 residue check script

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 1: Migration 0003 — DB-level "no enrolment without a consumed session"

The done-when demands a **DB-level** assertion that a failed session cannot produce an enrolment. Mechanism: `liveness_sessions` gets `UNIQUE (session_id, status)`; `enrolments` gets `session_status liveness_status NOT NULL DEFAULT 'consumed' CHECK (session_status = 'consumed')` and a composite FK `(session_id, session_status) → liveness_sessions(session_id, status)`. An enrolments row can therefore only reference a session whose *current* status is `'consumed'` — and only passed sessions are ever consumed. The FK also pins the session: flipping a consumed session's status while its enrolment exists is a constraint violation. Consequence for Task 3: within the transaction, the session UPDATE must run **before** the enrolment INSERT (the FK is checked per-statement).

**Files:**
- Create: `migrations/0003_enrolment_requires_consumed_session.up.sql`
- Create: `migrations/0003_enrolment_requires_consumed_session.down.sql`
- Test: `tests/test_enrolment_constraints.py`

**Interfaces:**
- Produces: `enrolments.session_status` column (always `'consumed'`, has a DEFAULT so Task 3's INSERT may omit it — but Task 3 sets it explicitly for clarity).

- [ ] **Step 1: Write the failing test**

Create `tests/test_enrolment_constraints.py`:

```python
"""Migration 0003: the DB itself refuses an enrolment for any session that is
not 'consumed' — a failed (or merely created/passed-unconsumed) session cannot
produce an enrolment even if application code is buggy (step-4 done-when:
"assert at the DB level, not just in application code").
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from tests.db import run_migrate

INSERT_ENROLMENT = """
    INSERT INTO enrolments
      (session_id, user_ref, collection_id, external_face_id, model_id,
       source_object_uri)
    VALUES (%s, %s, 'identity-v1', %s, 'rekognition:7.0',
            'https://proxy-s3.example/ref.jpg')
"""


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


def _insert_session(conn: psycopg.Connection, status: str) -> tuple:
    session_id, user_ref = uuid4(), uuid4()
    conn.execute(
        "INSERT INTO liveness_sessions"
        " (session_id, user_ref, provider_session_id, status, expires_at,"
        "  completed_at, consumed_at)"
        " VALUES (%s, %s, %s, %s::liveness_status, now() + interval '10 minutes',"
        "  CASE WHEN %s IN ('passed','failed','consumed') THEN now() END,"
        "  CASE WHEN %s = 'consumed' THEN now() END)",
        (session_id, user_ref, f"prov-{uuid4()}", status, status, status),
    )
    return session_id, user_ref


def test_failed_session_cannot_enrol(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        session_id, user_ref = _insert_session(conn, "failed")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(INSERT_ENROLMENT, (session_id, user_ref, f"face-{uuid4()}"))


def test_passed_but_unconsumed_session_cannot_enrol(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        session_id, user_ref = _insert_session(conn, "passed")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(INSERT_ENROLMENT, (session_id, user_ref, f"face-{uuid4()}"))


def test_consumed_session_can_enrol_exactly_once(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        session_id, user_ref = _insert_session(conn, "consumed")
        conn.execute(INSERT_ENROLMENT, (session_id, user_ref, f"face-{uuid4()}"))
        # UNIQUE FK on session_id: the single-use enforcement (CLAUDE.md §4 #2).
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(INSERT_ENROLMENT, (session_id, user_ref, f"face-{uuid4()}"))


def test_session_status_column_only_accepts_consumed(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        session_id, user_ref = _insert_session(conn, "failed")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO enrolments"
                " (session_id, session_status, user_ref, collection_id,"
                "  external_face_id, model_id, source_object_uri)"
                " VALUES (%s, 'failed', %s, 'identity-v1', %s, 'rekognition:7.0',"
                "  'https://proxy-s3.example/ref.jpg')",
                (session_id, user_ref, f"face-{uuid4()}"),
            )


def test_consumed_session_status_is_pinned_while_enrolment_exists(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        session_id, user_ref = _insert_session(conn, "consumed")
        conn.execute(INSERT_ENROLMENT, (session_id, user_ref, f"face-{uuid4()}"))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "UPDATE liveness_sessions SET status = 'passed' WHERE session_id = %s",
                (session_id,),
            )
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_enrolment_constraints.py -q`
Expected: FAIL — `ForeignKeyViolation` not raised (constraint doesn't exist yet). (`test_consumed_session_can_enrol_exactly_once` may already pass its unique half; the FK tests must fail.)

- [ ] **Step 3: Write the migration**

`migrations/0003_enrolment_requires_consumed_session.up.sql`:

```sql
-- Step 4 (CLAUDE.md §8): make "no enrolment without a passed liveness session"
-- (INVARIANTS #2) hold at the database level, not just in application code.
--
-- Mechanism: an enrolment row carries session_status, CHECK-pinned to
-- 'consumed', and a composite FK to (session_id, status). Only sessions whose
-- CURRENT status is 'consumed' can be referenced — and only passed sessions
-- are ever consumed (the quality-rejected path consumes without enrolling,
-- which this constraint permits: consumption without enrolment is legal,
-- enrolment without consumption is not). The FK also pins the session row:
-- its status cannot be changed away from 'consumed' while an enrolment
-- references it.
--
-- Ordering consequence for the writing transaction: UPDATE the session to
-- 'consumed' BEFORE inserting the enrolment (the FK is checked per statement).

ALTER TABLE liveness_sessions
  ADD CONSTRAINT liveness_sessions_session_id_status_key UNIQUE (session_id, status);

ALTER TABLE enrolments
  ADD COLUMN session_status liveness_status NOT NULL DEFAULT 'consumed'
    CONSTRAINT enrolments_session_status_consumed CHECK (session_status = 'consumed');

ALTER TABLE enrolments
  ADD CONSTRAINT enrolments_session_consumed_fk
  FOREIGN KEY (session_id, session_status)
  REFERENCES liveness_sessions (session_id, status);
```

`migrations/0003_enrolment_requires_consumed_session.down.sql`:

```sql
ALTER TABLE enrolments DROP CONSTRAINT enrolments_session_consumed_fk;
ALTER TABLE enrolments DROP COLUMN session_status;
ALTER TABLE liveness_sessions DROP CONSTRAINT liveness_sessions_session_id_status_key;
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_enrolment_constraints.py tests/test_migrations.py tests/test_schema_lint.py -q`
Expected: ALL PASS — including migrations (0003 is reversible) and the schema lint (`session_status` is an enum column; it passes gate (a)/(b)/(c)).

- [ ] **Step 5: Commit**

```bash
git add migrations/0003_enrolment_requires_consumed_session.up.sql migrations/0003_enrolment_requires_consumed_session.down.sql tests/test_enrolment_constraints.py
git commit -m "Migration 0003: enrolment requires a consumed session, enforced by composite FK

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `imageshield.enrolment` package — models, FaceIndex protocol, Rekognition implementation, wiring

**Files:**
- Create: `src/imageshield/enrolment/__init__.py` (empty)
- Create: `src/imageshield/enrolment/models.py`
- Create: `src/imageshield/enrolment/faceindex.py`
- Modify: `pyproject.toml` (per-file TID251 ignore for `faceindex.py`)
- Modify: `src/imageshield/http/deps.py` (add `get_face_index`, `get_enrolment_store`)
- Modify: `src/imageshield/http/app.py` (lifespan wiring)
- Test: `tests/test_faceindex.py`

**Interfaces:**
- Produces: `NewEnrolment`, `EnrolmentRow`, `IndexedFace`, `IndexRejected`, `FaceIndexUnavailable`, `QUALITY_REJECTED_REASON` (all in `imageshield.enrolment.models`); `FaceIndex` protocol with `index_face(*, collection_id: str, external_image_id: str, image_bytes: bytes) -> IndexedFace | IndexRejected`, `delete_faces(collection_id: str, face_ids: tuple[str, ...]) -> None`, `list_face_ids(collection_id: str, face_ids: tuple[str, ...]) -> tuple[str, ...]`; deps `get_face_index`, `get_enrolment_store` (the latter's implementation lands in Task 5 — dep function references `app.state.enrolment_store` generically, so it can be written now).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_faceindex.py` — the boto3 client is faked at the client-object seam (same pattern `RekognitionLivenessProvider` uses via its injectable `client`):

```python
"""RekognitionFaceIndex against a stubbed boto3 client.

Pins the four things the step-4 brief makes load-bearing:
- IndexFaces is called with QualityFilter='HIGH', MaxFaces=1,
  DetectionAttributes=[], ExternalImageId = user_ref, and the IN-MEMORY bytes;
- quality rejection (empty FaceRecords) maps to IndexRejected with reasons;
- AWS ClientError maps to FaceIndexUnavailable (route turns that into 503);
- ListFaces paginates and filters on the FaceIds we ask about.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from imageshield.enrolment.faceindex import RekognitionFaceIndex
from imageshield.enrolment.models import FaceIndexUnavailable, IndexedFace, IndexRejected


class StubClient:
    def __init__(self) -> None:
        self.index_kwargs: list[dict[str, Any]] = []
        self.delete_kwargs: list[dict[str, Any]] = []
        self.list_kwargs: list[dict[str, Any]] = []
        self.index_response: Any = {
            "FaceRecords": [{"Face": {"FaceId": "face-1", "Confidence": 99.9}}],
            "FaceModelVersion": "7.0",
        }
        self.list_responses: list[dict[str, Any]] = [{"Faces": []}]

    def index_faces(self, **kwargs: Any) -> dict[str, Any]:
        self.index_kwargs.append(kwargs)
        if isinstance(self.index_response, Exception):
            raise self.index_response
        return self.index_response

    def delete_faces(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_kwargs.append(kwargs)
        return {}

    def list_faces(self, **kwargs: Any) -> dict[str, Any]:
        self.list_kwargs.append(kwargs)
        return self.list_responses.pop(0)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "IndexFaces")


async def test_index_face_sends_the_exact_contract() -> None:
    stub = StubClient()
    index = RekognitionFaceIndex(region="us-east-1", client=stub)

    result = await index.index_face(
        collection_id="identity-v1",
        external_image_id="11111111-1111-1111-1111-111111111111",
        image_bytes=b"jpeg-bytes",
    )

    assert isinstance(result, IndexedFace)
    assert result.face_id == "face-1"
    assert result.quality_score == 99.9
    assert result.model_id == "rekognition:7.0"
    (kwargs,) = stub.index_kwargs
    assert kwargs == {
        "CollectionId": "identity-v1",
        "ExternalImageId": "11111111-1111-1111-1111-111111111111",
        "QualityFilter": "HIGH",
        "MaxFaces": 1,
        "DetectionAttributes": [],
        "Image": {"Bytes": b"jpeg-bytes"},
    }


async def test_quality_rejection_maps_to_reasons() -> None:
    stub = StubClient()
    stub.index_response = {
        "FaceRecords": [],
        "UnindexedFaces": [{"Reasons": ["LOW_SHARPNESS", "LOW_BRIGHTNESS"]}],
        "FaceModelVersion": "7.0",
    }
    index = RekognitionFaceIndex(region="us-east-1", client=stub)

    result = await index.index_face(
        collection_id="identity-v1", external_image_id="u", image_bytes=b"x"
    )

    assert result == IndexRejected(reasons=("LOW_SHARPNESS", "LOW_BRIGHTNESS"))


async def test_no_face_detected_is_a_rejection_not_a_crash() -> None:
    stub = StubClient()
    stub.index_response = {"FaceRecords": [], "UnindexedFaces": [], "FaceModelVersion": "7.0"}
    index = RekognitionFaceIndex(region="us-east-1", client=stub)

    result = await index.index_face(
        collection_id="identity-v1", external_image_id="u", image_bytes=b"x"
    )

    assert result == IndexRejected(reasons=("NO_FACE_DETECTED",))


async def test_client_error_maps_to_unavailable() -> None:
    stub = StubClient()
    stub.index_response = _client_error("ThrottlingException")
    index = RekognitionFaceIndex(region="us-east-1", client=stub)

    with pytest.raises(FaceIndexUnavailable):
        await index.index_face(
            collection_id="identity-v1", external_image_id="u", image_bytes=b"x"
        )


async def test_list_face_ids_paginates_and_returns_survivors() -> None:
    stub = StubClient()
    stub.list_responses = [
        {"Faces": [{"FaceId": "face-1"}], "NextToken": "t1"},
        {"Faces": [{"FaceId": "face-2"}]},
    ]
    index = RekognitionFaceIndex(region="us-east-1", client=stub)

    survivors = await index.list_face_ids("identity-v1", ("face-1", "face-2", "face-3"))

    assert survivors == ("face-1", "face-2")
    assert stub.list_kwargs[0]["FaceIds"] == ["face-1", "face-2", "face-3"]
    assert stub.list_kwargs[1]["NextToken"] == "t1"


async def test_collection_gone_means_nothing_searchable() -> None:
    stub = StubClient()
    stub.list_responses = []
    stub.list_faces = lambda **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "ListFaces")
    )
    index = RekognitionFaceIndex(region="us-east-1", client=stub)

    assert await index.list_face_ids("identity-v1", ("face-1",)) == ()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_faceindex.py -q`
Expected: FAIL with `ModuleNotFoundError: imageshield.enrolment`.

- [ ] **Step 3: Implement the package**

`src/imageshield/enrolment/__init__.py`: empty file.

`src/imageshield/enrolment/models.py`:

```python
"""Domain types for enrolment (CLAUDE.md §8 step 4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

# Stored in liveness_sessions.failure_reason when IndexFaces' HIGH quality
# filter rejects the frame, and surfaced verbatim as the response `reason` so
# the proxy can tell "liveness passed, enrolment didn't" apart and start a
# fresh session (step-4 brief).
QUALITY_REJECTED_REASON = "quality_rejected"


@dataclass(frozen=True, slots=True)
class NewEnrolment:
    """What the route hands the store after a successful IndexFaces."""

    user_ref: UUID
    collection_id: str
    external_face_id: str
    quality_score: float | None
    model_id: str
    source_object_uri: str


@dataclass(frozen=True, slots=True)
class EnrolmentRow:
    """One row of ``enrolments``, as the store returns it."""

    enrolment_id: UUID
    session_id: UUID
    user_ref: UUID
    collection_id: str
    external_face_id: str
    quality_score: float | None
    model_id: str
    source_object_uri: str
    status: str
    created_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class IndexedFace:
    """IndexFaces accepted the frame: the FaceId now in the collection, the
    detection confidence (quality_score), and the face model version that
    produced the vector (INVARIANTS #4 — every vector-bearing row carries
    model_id)."""

    face_id: str
    quality_score: float | None
    model_id: str


@dataclass(frozen=True, slots=True)
class IndexRejected:
    """The HIGH quality filter rejected the frame. ``reasons`` comes from
    ``UnindexedFaces[].Reasons`` (or NO_FACE_DETECTED when both lists were
    empty) — logged for ops, never stored per-reason."""

    reasons: tuple[str, ...]


class FaceIndexUnavailable(RuntimeError):
    """A Rekognition face-index call failed. The route maps this to 503:
    nothing was written, the session is not consumed, and the proxy retries
    the whole result call with the same Idempotency-Key."""
```

`src/imageshield/enrolment/faceindex.py`:

```python
"""Rekognition collection operations: IndexFaces, DeleteFaces, ListFaces.

The third sanctioned boto3 importer (pyproject.toml TID251 per-file-ignores),
alongside the relay and the liveness provider. No S3 client — bytes arrive
in memory from ``GetFaceLivenessSessionResults`` and are never re-fetched.

Hard rules enforced by construction (step-4 brief, CLAUDE.md §4):
- ``QualityFilter='HIGH'`` — never AUTO. A poor enrolment vector degrades
  every match the user will ever get, permanently.
- ``ExternalImageId`` is the caller-supplied ``user_ref`` and nothing else.
- There is no search method on this protocol AT ALL. Identity comes from the
  request; the old system's search-by-face call is the fragmentation bug
  (INVARIANTS #1), and the step-9 CI grep hunts for that API's name — which
  is deliberately not written out here, so the grep stays clean.

Every ClientError maps to :class:`FaceIndexUnavailable` (route → 503,
retryable). A permanently failing call therefore surfaces as repeated 503s
with the AWS error code in the log — acceptable for v1, where the only
callers are the proxy's bounded retries.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

# TID251 per-file ignore (pyproject.toml): Rekognition collection ops only.
import boto3
import structlog
from botocore.exceptions import ClientError

from imageshield.enrolment.models import FaceIndexUnavailable, IndexedFace, IndexRejected

log = structlog.get_logger("imageshield.enrolment")


class FaceIndex(Protocol):
    async def index_face(
        self, *, collection_id: str, external_image_id: str, image_bytes: bytes
    ) -> IndexedFace | IndexRejected: ...

    async def delete_faces(self, collection_id: str, face_ids: tuple[str, ...]) -> None: ...

    async def list_face_ids(
        self, collection_id: str, face_ids: tuple[str, ...]
    ) -> tuple[str, ...]: ...


class RekognitionFaceIndex:
    """boto3-backed implementation; blocking calls run via asyncio.to_thread."""

    def __init__(self, *, region: str, client: Any | None = None) -> None:
        self._client = client if client is not None else boto3.client(
            "rekognition", region_name=region
        )

    async def index_face(
        self, *, collection_id: str, external_image_id: str, image_bytes: bytes
    ) -> IndexedFace | IndexRejected:
        try:
            response = await asyncio.to_thread(
                self._client.index_faces,
                CollectionId=collection_id,
                ExternalImageId=external_image_id,
                QualityFilter="HIGH",
                MaxFaces=1,
                DetectionAttributes=[],
                Image={"Bytes": image_bytes},
            )
        except ClientError as exc:
            raise self._unavailable("IndexFaces", exc) from exc

        records = response.get("FaceRecords") or []
        if not records:
            reasons = tuple(
                str(reason)
                for unindexed in response.get("UnindexedFaces") or []
                for reason in unindexed.get("Reasons") or []
            )
            return IndexRejected(reasons=reasons or ("NO_FACE_DETECTED",))

        face = records[0]["Face"]
        confidence = face.get("Confidence")
        return IndexedFace(
            face_id=str(face["FaceId"]),
            quality_score=float(confidence) if confidence is not None else None,
            model_id=f"rekognition:{response.get('FaceModelVersion', 'unknown')}",
        )

    async def delete_faces(self, collection_id: str, face_ids: tuple[str, ...]) -> None:
        try:
            await asyncio.to_thread(
                self._client.delete_faces,
                CollectionId=collection_id,
                FaceIds=list(face_ids),
            )
        except ClientError as exc:
            if self._error_code(exc) == "ResourceNotFoundException":
                return  # collection gone -> nothing searchable, which is the goal
            raise self._unavailable("DeleteFaces", exc) from exc

    async def list_face_ids(
        self, collection_id: str, face_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        found: list[str] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "CollectionId": collection_id,
                "FaceIds": list(face_ids),
            }
            if next_token is not None:
                kwargs["NextToken"] = next_token
            try:
                response = await asyncio.to_thread(self._client.list_faces, **kwargs)
            except ClientError as exc:
                if self._error_code(exc) == "ResourceNotFoundException":
                    return ()
                raise self._unavailable("ListFaces", exc) from exc
            found.extend(str(face["FaceId"]) for face in response.get("Faces") or [])
            next_token = response.get("NextToken")
            if next_token is None:
                return tuple(found)

    @staticmethod
    def _error_code(exc: ClientError) -> str:
        return str(exc.response.get("Error", {}).get("Code", ""))

    def _unavailable(self, operation: str, exc: ClientError) -> FaceIndexUnavailable:
        code = self._error_code(exc)
        log.warning("faceindex.call_failed", operation=operation, error_code=code)
        return FaceIndexUnavailable(f"{operation} failed with {code or 'unknown error'}")
```

- [ ] **Step 4: Sanction the boto3 import and wire the app**

`pyproject.toml` — extend the per-file ignores (and update the banned-api message's list of sanctioned importers to mention it):

```toml
[tool.ruff.lint.per-file-ignores]
# The three sanctioned boto3 importers:
# - the relay (Task 4): the consumer side of the outbox pattern, the only
#   place that calls SendMessage;
# - the liveness provider (step 3): Rekognition Face Liveness only;
# - the face index (step 4): Rekognition collection ops (IndexFaces /
#   DeleteFaces / ListFaces) only. No S3 client anywhere — presigned URLs via
#   httpx are the only S3 path.
"src/imageshield/relay.py" = ["TID251"]
"src/imageshield/liveness/provider.py" = ["TID251"]
"src/imageshield/enrolment/faceindex.py" = ["TID251"]
```

`src/imageshield/http/deps.py` — append (and add the TYPE_CHECKING imports `from imageshield.enrolment.faceindex import FaceIndex`, `from imageshield.enrolment.store import EnrolmentStore`):

```python
def get_face_index(request: Request) -> FaceIndex:
    face_index: FaceIndex = _required_state(request, "face_index")  # type: ignore[assignment]
    return face_index


def get_enrolment_store(request: Request) -> EnrolmentStore:
    store: EnrolmentStore = _required_state(request, "enrolment_store")  # type: ignore[assignment]
    return store
```

(`enrolment/store.py` with the `EnrolmentStore` protocol lands in Task 5; to keep this task self-contained and mypy green, create `src/imageshield/enrolment/store.py` NOW containing only the protocol — Task 5 adds the Postgres implementation:)

```python
"""Persistence for ``enrolments`` — raw SQL, no ORM (CLAUDE.md §2)."""

from __future__ import annotations

from typing import Protocol

from imageshield.enrolment.models import EnrolmentRow
from imageshield.types import UserRef


class EnrolmentStore(Protocol):
    async def get_active_enrolments(self, user_ref: UserRef) -> tuple[EnrolmentRow, ...]: ...

    async def tombstone_enrolments(self, user_ref: UserRef) -> int: ...
```

`src/imageshield/http/app.py` — in `_lifespan`, after the uploader block:

```python
    if getattr(app.state, "face_index", None) is None:
        app.state.face_index = RekognitionFaceIndex(region=cfg.aws_region)
    if getattr(app.state, "enrolment_store", None) is None:
        app.state.enrolment_store = PostgresEnrolmentStore(pool)
```

with imports `from imageshield.enrolment.faceindex import RekognitionFaceIndex` and (Task 5) `from imageshield.enrolment.store import PostgresEnrolmentStore`. **In this task**, wire only `face_index`; add the `enrolment_store` line in Task 5 when `PostgresEnrolmentStore` exists.

- [ ] **Step 5: Run tests, mypy, ruff**

Run: `python -m pytest tests/test_faceindex.py -q && python -m mypy && python -m ruff check src tests`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add src/imageshield/enrolment pyproject.toml src/imageshield/http/deps.py src/imageshield/http/app.py tests/test_faceindex.py
git commit -m "Enrolment domain: FaceIndex protocol + Rekognition implementation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Transactional finalizers — `finalize_enrolled` / `finalize_quality_rejected`

ONE transaction: consume the session (UPDATE first — the composite FK demands it), insert the enrolment, `NOTIFY enrolment_complete`. The quality-rejected variant consumes without inserting. Both are guarded by `completed_at IS NULL` and return `None` when a concurrent call already finalized — the route uses that to compensate (Task 4).

**Files:**
- Modify: `src/imageshield/liveness/store.py`
- Test: `tests/test_liveness_store.py` (append)

**Interfaces:**
- Consumes: `NewEnrolment`, `EnrolmentRow`, `QUALITY_REJECTED_REASON` from `imageshield.enrolment.models`.
- Produces (added to `LivenessStore` protocol AND `PostgresLivenessStore`):
  - `async def finalize_enrolled(self, session_id: SessionId, *, confidence: float | None, reference_image_uri: str, audit_image_uris: tuple[str, ...], enrolment: NewEnrolment) -> tuple[LivenessSessionRow, EnrolmentRow] | None`
  - `async def finalize_quality_rejected(self, session_id: SessionId, *, confidence: float | None, reference_image_uri: str, audit_image_uris: tuple[str, ...]) -> LivenessSessionRow | None`

- [ ] **Step 1: Write the failing tests (append to `tests/test_liveness_store.py`)**

```python
# --- Step 4: finalize_enrolled / finalize_quality_rejected -------------------

import asyncio

from imageshield.enrolment.models import QUALITY_REJECTED_REASON, NewEnrolment


def _new_enrolment(user_ref: object) -> NewEnrolment:
    return NewEnrolment(
        user_ref=user_ref,  # type: ignore[arg-type]
        collection_id="identity-v1",
        external_face_id=f"face-{uuid4()}",
        quality_score=99.5,
        model_id="rekognition:7.0",
        source_object_uri="https://proxy-s3.example/ref.jpg",
    )


async def test_finalize_enrolled_consumes_and_inserts_atomically(
    store: PostgresLivenessStore,
) -> None:
    row = await _create(store)
    await store.claim_result(row.session_id, "idem-1")

    outcome = await store.finalize_enrolled(
        row.session_id,
        confidence=98.7,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=("https://proxy-s3.example/audit-0.jpg",),
        enrolment=_new_enrolment(row.user_ref),
    )

    assert outcome is not None
    session, enrolment = outcome
    assert session.status == "consumed"
    assert session.consumed_at is not None
    assert session.completed_at is not None
    assert session.failure_reason is None
    assert session.confidence == 98.7
    assert enrolment.session_id == row.session_id
    assert enrolment.user_ref == row.user_ref
    assert enrolment.status == "active"
    assert enrolment.model_id == "rekognition:7.0"


async def test_finalize_enrolled_returns_none_when_already_finalized(
    store: PostgresLivenessStore,
) -> None:
    row = await _create(store)
    await store.finalize_result(
        row.session_id,
        status="failed",
        confidence=10.0,
        failure_reason="provider_reported_failure",
        reference_image_uri=None,
        audit_image_uris=None,
    )

    outcome = await store.finalize_enrolled(
        row.session_id,
        confidence=98.7,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=(),
        enrolment=_new_enrolment(row.user_ref),
    )

    assert outcome is None  # and, per the FK, no enrolment row can exist
    refetched = await store.get_session(row.session_id)
    assert refetched is not None and refetched.status == "failed"


async def test_finalize_quality_rejected_consumes_without_enrolment(
    store: PostgresLivenessStore, migrated_db: str
) -> None:
    row = await _create(store)

    session = await store.finalize_quality_rejected(
        row.session_id,
        confidence=97.0,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=(),
    )

    assert session is not None
    assert session.status == "consumed"
    assert session.consumed_at is not None
    assert session.failure_reason == QUALITY_REJECTED_REASON
    async with await psycopg.AsyncConnection.connect(migrated_db) as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM enrolments WHERE session_id = %s", (row.session_id,)
        )
        record = await cur.fetchone()
        assert record is not None and record[0] == 0


async def test_finalize_enrolled_emits_notify_in_the_transaction(
    store: PostgresLivenessStore, migrated_db: str
) -> None:
    row = await _create(store)
    async with await psycopg.AsyncConnection.connect(
        migrated_db, autocommit=True
    ) as listener:
        await listener.execute("LISTEN enrolment_complete")

        await store.finalize_enrolled(
            row.session_id,
            confidence=98.7,
            reference_image_uri="https://proxy-s3.example/ref.jpg",
            audit_image_uris=(),
            enrolment=_new_enrolment(row.user_ref),
        )

        gen = listener.notifies()
        notification = await asyncio.wait_for(anext(gen), timeout=5)
        await gen.aclose()
    assert notification.payload == str(row.session_id)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_liveness_store.py -q -k "finalize_enrolled or quality_rejected"`
Expected: FAIL with `AttributeError: ... has no attribute 'finalize_enrolled'`.

- [ ] **Step 3: Implement in `src/imageshield/liveness/store.py`**

Add imports to `liveness/store.py`:

```python
from imageshield.enrolment.models import QUALITY_REJECTED_REASON, EnrolmentRow, NewEnrolment
from imageshield.enrolment.store import to_enrolment_row
```

Add SQL constants (near the existing ones):

```python
# Consumption: UPDATE must precede the enrolment INSERT in the same
# transaction — migration 0003's composite FK requires the session's CURRENT
# status to be 'consumed' at insert time. completed_at IS NULL guards against
# a concurrent finalizer: the second writer sees zero rows and backs off.
_CONSUME_SQL = f"""
    UPDATE liveness_sessions
    SET status = 'consumed',
        confidence = %(confidence)s,
        failure_reason = %(failure_reason)s,
        reference_image_uri = %(reference_image_uri)s,
        audit_image_uris = %(audit_image_uris)s,
        completed_at = now(),
        consumed_at = now()
    WHERE session_id = %(session_id)s AND completed_at IS NULL
    RETURNING {_COLUMNS}
"""

_ENROLMENT_COLUMNS = (
    "enrolment_id, session_id, user_ref, collection_id, external_face_id,"
    " quality_score, model_id, source_object_uri, status, created_at, deleted_at"
)

_INSERT_ENROLMENT_SQL = f"""
    INSERT INTO enrolments
      (session_id, session_status, user_ref, collection_id, external_face_id,
       quality_score, model_id, source_object_uri)
    VALUES
      (%(session_id)s, 'consumed', %(user_ref)s, %(collection_id)s,
       %(external_face_id)s, %(quality_score)s, %(model_id)s,
       %(source_object_uri)s)
    RETURNING {_ENROLMENT_COLUMNS}
"""

_NOTIFY_SQL = "SELECT pg_notify('enrolment_complete', %(session_id)s::text)"
```

Add a row mapper — this goes in `src/imageshield/enrolment/store.py` (the enrolments table's own module, created protocol-only in Task 2; Task 5's `PostgresEnrolmentStore` reuses it), and `liveness/store.py` imports it with `from imageshield.enrolment.store import to_enrolment_row`. It needs `from decimal import Decimal`, `from typing import Any` and `from imageshield.enrolment.models import EnrolmentRow` in that module:

```python
def to_enrolment_row(record: tuple[Any, ...]) -> EnrolmentRow:
    (
        enrolment_id,
        session_id,
        user_ref,
        collection_id,
        external_face_id,
        quality_score,
        model_id,
        source_object_uri,
        status,
        created_at,
        deleted_at,
    ) = record
    return EnrolmentRow(
        enrolment_id=enrolment_id,
        session_id=session_id,
        user_ref=user_ref,
        collection_id=collection_id,
        external_face_id=external_face_id,
        quality_score=(
            float(quality_score) if isinstance(quality_score, Decimal) else quality_score
        ),
        model_id=model_id,
        source_object_uri=source_object_uri,
        status=str(status),
        created_at=created_at,
        deleted_at=deleted_at,
    )
```

Add to the `LivenessStore` Protocol:

```python
    async def finalize_enrolled(
        self,
        session_id: SessionId,
        *,
        confidence: float | None,
        reference_image_uri: str,
        audit_image_uris: tuple[str, ...],
        enrolment: NewEnrolment,
    ) -> tuple[LivenessSessionRow, EnrolmentRow] | None: ...

    async def finalize_quality_rejected(
        self,
        session_id: SessionId,
        *,
        confidence: float | None,
        reference_image_uri: str,
        audit_image_uris: tuple[str, ...],
    ) -> LivenessSessionRow | None: ...
```

Add to `PostgresLivenessStore`:

```python
    async def finalize_enrolled(
        self,
        session_id: SessionId,
        *,
        confidence: float | None,
        reference_image_uri: str,
        audit_image_uris: tuple[str, ...],
        enrolment: NewEnrolment,
    ) -> tuple[LivenessSessionRow, EnrolmentRow] | None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _CONSUME_SQL,
                {
                    "session_id": session_id,
                    "confidence": confidence,
                    "failure_reason": None,
                    "reference_image_uri": reference_image_uri,
                    "audit_image_uris": list(audit_image_uris),
                },
            )
            session_record = await cur.fetchone()
            if session_record is None:
                return None  # concurrent finalizer won; caller compensates
            cur = await conn.execute(
                _INSERT_ENROLMENT_SQL,
                {
                    "session_id": session_id,
                    "user_ref": enrolment.user_ref,
                    "collection_id": enrolment.collection_id,
                    "external_face_id": enrolment.external_face_id,
                    "quality_score": enrolment.quality_score,
                    "model_id": enrolment.model_id,
                    "source_object_uri": enrolment.source_object_uri,
                },
            )
            enrolment_record = await cur.fetchone()
            assert enrolment_record is not None
            await conn.execute(_NOTIFY_SQL, {"session_id": session_id})
        return _to_row(session_record), to_enrolment_row(enrolment_record)

    async def finalize_quality_rejected(
        self,
        session_id: SessionId,
        *,
        confidence: float | None,
        reference_image_uri: str,
        audit_image_uris: tuple[str, ...],
    ) -> LivenessSessionRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _CONSUME_SQL,
                {
                    "session_id": session_id,
                    "confidence": confidence,
                    "failure_reason": QUALITY_REJECTED_REASON,
                    "reference_image_uri": reference_image_uri,
                    "audit_image_uris": list(audit_image_uris),
                },
            )
            record = await cur.fetchone()
        return _to_row(record) if record is not None else None
```

- [ ] **Step 4: Run the store tests**

Run: `python -m pytest tests/test_liveness_store.py tests/test_enrolment_constraints.py -q && python -m mypy`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/imageshield/liveness/store.py tests/test_liveness_store.py
git commit -m "Liveness store: transactional finalize_enrolled / finalize_quality_rejected with NOTIFY

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Result route — index on pass, quality rejection, transient 503, replay semantics

**Files:**
- Modify: `src/imageshield/http/models.py` (`reason` field; drop the "always False" comments)
- Modify: `src/imageshield/http/routes/liveness.py`
- Test: `tests/test_liveness_routes.py` (extend fakes + new tests)

**Interfaces:**
- Consumes: `FaceIndex` (`get_face_index` dep), `finalize_enrolled` / `finalize_quality_rejected`, `IndexedFace` / `IndexRejected` / `FaceIndexUnavailable` / `QUALITY_REJECTED_REASON` / `NewEnrolment`.
- Produces: response contract — enrolled: `200 {status:'passed', confidence, enrolled:true}`; quality-rejected: `200 {status:'passed', confidence, enrolled:false, reason:'quality_rejected'}`; transient: `503 face_index_unavailable` (retryable); replay/GET derive `enrolled = consumed_at is not None and failure_reason is None`.

- [ ] **Step 1: Extend the fakes and write the failing tests**

In `tests/test_liveness_routes.py`:

Add imports: `from imageshield.enrolment.models import FaceIndexUnavailable, IndexedFace, IndexRejected, NewEnrolment, QUALITY_REJECTED_REASON, EnrolmentRow`.

Extend `FakeLivenessStore` with the two finalizers and an enrolments dict:

```python
    # in __init__:
        self.enrolments: dict[UUID, EnrolmentRow] = {}  # keyed by session_id
        self.notifies: list[str] = []

    async def finalize_enrolled(
        self,
        session_id: UUID,
        *,
        confidence: float | None,
        reference_image_uri: str,
        audit_image_uris: tuple[str, ...],
        enrolment: NewEnrolment,
    ) -> tuple[LivenessSessionRow, Any] | None:
        if self.rows[session_id].completed_at is not None:
            return None
        row = self.set(
            session_id,
            status="consumed",
            confidence=confidence,
            failure_reason=None,
            reference_image_uri=reference_image_uri,
            audit_image_uris=audit_image_uris,
            completed_at=_now(),
            consumed_at=_now(),
        )
        enrolment_row = EnrolmentRow(
            enrolment_id=uuid4(),
            session_id=session_id,
            user_ref=enrolment.user_ref,
            collection_id=enrolment.collection_id,
            external_face_id=enrolment.external_face_id,
            quality_score=enrolment.quality_score,
            model_id=enrolment.model_id,
            source_object_uri=enrolment.source_object_uri,
            status="active",
            created_at=_now(),
            deleted_at=None,
        )
        self.enrolments[session_id] = enrolment_row
        self.notifies.append(str(session_id))
        return row, enrolment_row

    async def finalize_quality_rejected(
        self,
        session_id: UUID,
        *,
        confidence: float | None,
        reference_image_uri: str,
        audit_image_uris: tuple[str, ...],
    ) -> LivenessSessionRow | None:
        if self.rows[session_id].completed_at is not None:
            return None
        return self.set(
            session_id,
            status="consumed",
            confidence=confidence,
            failure_reason=QUALITY_REJECTED_REASON,
            reference_image_uri=reference_image_uri,
            audit_image_uris=audit_image_uris,
            completed_at=_now(),
            consumed_at=_now(),
        )
```

Add a `FakeFaceIndex` (note: it has NO search method at all — that absence is structural):

```python
class FakeFaceIndex:
    """In-memory Rekognition collection. Every accepted index mints a fresh
    FaceId — exactly like the real thing, which is why two lookalikes can
    never collapse into one identity here: nothing ever searches."""

    def __init__(self) -> None:
        self.index_calls: list[dict[str, Any]] = []
        self.faces: dict[str, tuple[str, str]] = {}  # face_id -> (collection, external_image_id)
        self.next_result: IndexRejected | Exception | None = None
        self._counter = 0

    async def index_face(
        self, *, collection_id: str, external_image_id: str, image_bytes: bytes
    ) -> IndexedFace | IndexRejected:
        self.index_calls.append(
            {
                "collection_id": collection_id,
                "external_image_id": external_image_id,
                "image_bytes": image_bytes,
            }
        )
        if isinstance(self.next_result, Exception):
            raise self.next_result
        if self.next_result is not None:
            return self.next_result
        self._counter += 1
        face_id = f"face-{self._counter}"
        self.faces[face_id] = (collection_id, external_image_id)
        return IndexedFace(face_id=face_id, quality_score=99.0, model_id="rekognition:7.0")

    async def delete_faces(self, collection_id: str, face_ids: tuple[str, ...]) -> None:
        for face_id in face_ids:
            self.faces.pop(face_id, None)

    async def list_face_ids(
        self, collection_id: str, face_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        return tuple(face_id for face_id in face_ids if face_id in self.faces)
```

In `Harness.__init__`, add `self.face_index = FakeFaceIndex()` and `app.state.face_index = self.face_index`.

Add a harness helper that walks a session to a provider-passed state (reuse the file's existing helpers if equivalents already exist — read the rest of the file first):

```python
    def passed_provider_result(self, row: LivenessSessionRow, confidence: float = 98.7) -> None:
        self.provider.results[row.provider_session_id] = ProviderResult(
            status="succeeded",
            confidence=confidence,
            reference_image=b"reference-jpeg",
            audit_images=(b"audit-0", b"audit-1"),
        )
```

New tests:

```python
# --- Step 4: enrolment on pass ------------------------------------------------


def test_passed_session_enrols_and_consumes() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row)

    response = h.result(row.session_id)

    assert response.status_code == 200
    assert response.json() == {
        "status": "passed",
        "confidence": 98.7,
        "enrolled": True,
        "reason": None,
    }
    (call,) = h.face_index.index_calls
    assert call["external_image_id"] == str(row.user_ref)  # ExternalImageId = user_ref
    assert call["collection_id"] == "identity-v1"
    assert call["image_bytes"] == b"reference-jpeg"  # the in-memory bytes, not a re-fetch
    stored = h.store.rows[row.session_id]
    assert stored.status == "consumed" and stored.consumed_at is not None
    enrolment = h.store.enrolments[row.session_id]
    assert enrolment.source_object_uri == "https://proxy-s3.example/ref.jpg"
    assert h.store.notifies == [str(row.session_id)]


def test_lookalike_users_enrol_as_two_distinct_identities() -> None:
    """PERMANENT TEST — never delete (step-4 done-when; CLAUDE.md §10).

    Two lookalike faces enrol as two distinct user_refs, each with its own
    external_face_id. This is the regression guard for the fragmentation bug
    class that leaks one user's sexual-content matches to another: the old
    system searched the collection first and minted/overwrote identity from a
    similarity score (server.js:9585). Identity here comes from the request,
    every index call mints a fresh FaceId, and FakeFaceIndex — like the
    production FaceIndex protocol — has no search method at all.
    """
    h = Harness()
    user_a, user_b = uuid4(), uuid4()
    lookalike = b"nearly-identical-face-jpeg"
    row_a = h.store.add(make_row(user_ref=user_a))
    row_b = h.store.add(make_row(user_ref=user_b))
    for row in (row_a, row_b):
        h.provider.results[row.provider_session_id] = ProviderResult(
            status="succeeded",
            confidence=99.0,
            reference_image=lookalike,
            audit_images=(),
        )

    first = h.result(row_a.session_id, key="idem-a")
    second = h.result(row_b.session_id, key="idem-b")

    assert first.status_code == 200 and first.json()["enrolled"] is True
    assert second.status_code == 200 and second.json()["enrolled"] is True
    enrolment_a = h.store.enrolments[row_a.session_id]
    enrolment_b = h.store.enrolments[row_b.session_id]
    assert enrolment_a.user_ref == user_a
    assert enrolment_b.user_ref == user_b
    assert enrolment_a.external_face_id != enrolment_b.external_face_id
    bindings = {v[1] for v in h.face_index.faces.values()}
    assert bindings == {str(user_a), str(user_b)}
    assert not hasattr(h.face_index, "search_faces")  # no search path exists to collapse them


def test_quality_rejection_consumes_without_enrolment() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row)
    h.face_index.next_result = IndexRejected(reasons=("LOW_SHARPNESS",))

    response = h.result(row.session_id)

    assert response.status_code == 200
    assert response.json() == {
        "status": "passed",
        "confidence": 98.7,
        "enrolled": False,
        "reason": "quality_rejected",
    }
    stored = h.store.rows[row.session_id]
    assert stored.status == "consumed" and stored.consumed_at is not None
    assert row.session_id not in h.store.enrolments
    # Consumed, so the next result call is 410 and a FRESH create is allowed
    # (no passed-unconsumed 409 lockout).
    assert h.create(row.user_ref).status_code == 201


def test_transient_index_failure_returns_503_and_consumes_nothing() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row)
    h.face_index.next_result = FaceIndexUnavailable("IndexFaces failed with ThrottlingException")

    response = h.result(row.session_id)

    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "face_index_unavailable" and body["retryable"] is True
    stored = h.store.rows[row.session_id]
    assert stored.completed_at is None and stored.consumed_at is None
    assert row.session_id not in h.store.enrolments

    # The proxy retries the whole call with the SAME key once AWS recovers.
    h.face_index.next_result = None
    retry = h.result(row.session_id)
    assert retry.status_code == 200 and retry.json()["enrolled"] is True


def test_same_key_replay_after_enrolment_does_not_double_index() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row)
    assert h.result(row.session_id, key="idem-1").status_code == 200

    replay = h.result(row.session_id, key="idem-1")

    assert replay.status_code == 200
    assert replay.json()["enrolled"] is True
    assert len(h.face_index.index_calls) == 1  # replay did NOT re-index
    assert len(h.store.enrolments) == 1


def test_different_key_after_enrolment_is_410() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row)
    assert h.result(row.session_id, key="idem-1").status_code == 200

    replay = h.result(row.session_id, key="idem-2")

    assert replay.status_code == 410
    assert len(h.face_index.index_calls) == 1


def test_failed_session_never_reaches_the_index() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.provider.results[row.provider_session_id] = ProviderResult(
        status="failed", confidence=12.0, reference_image=None, audit_images=()
    )

    response = h.result(row.session_id)

    assert response.status_code == 200
    assert response.json()["enrolled"] is False
    assert h.face_index.index_calls == []


def test_get_status_reports_enrolled() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row)
    h.result(row.session_id)

    response = h.client.get(f"/v1/liveness/{row.session_id}", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"status": "consumed", "confidence": 98.7, "enrolled": True}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_liveness_routes.py -q`
Expected: new tests FAIL (`enrolled` False / missing `reason` / no `face_index` dep). Pre-existing tests that assert `"enrolled": False` in passed responses will also fail once the route changes — update those existing assertions as part of Step 3, not by weakening them but by reflecting the new contract (a passed upload path now enrols; where an old test wants "passed but not enrolled", give its harness `h.face_index.next_result = IndexRejected(...)` or assert the new fields explicitly).

- [ ] **Step 3: Implement**

`src/imageshield/http/models.py` — replace `LivenessResultResponse` and `LivenessStatusResponse`:

```python
class LivenessResultResponse(BaseModel):
    status: Literal["passed", "failed"]
    confidence: float | None
    # True iff IndexFaces accepted the ReferenceImage and the enrolments row
    # was written (step 4). 'passed' + enrolled=False + reason tells the proxy
    # to start a FRESH liveness session.
    enrolled: bool = False
    reason: Literal["quality_rejected"] | None = None


class LivenessStatusResponse(BaseModel):
    status: Literal["created", "pending", "passed", "failed", "expired", "consumed"]
    confidence: float | None
    enrolled: bool = False
```

`src/imageshield/http/routes/liveness.py`:

Update the module docstring (drop "``enrolled`` is always False here. Indexing is step 4.", describe the step-4 flow in two or three lines). Add imports:

```python
from imageshield.enrolment.faceindex import FaceIndex
from imageshield.enrolment.models import (
    QUALITY_REJECTED_REASON,
    FaceIndexUnavailable,
    IndexRejected,
    NewEnrolment,
)
from imageshield.http.deps import get_face_index
from imageshield.types import UserRef
```

Replace `_result_response` and add `_enrolled` (a consumed session was necessarily passed — only the enrolment/quality paths consume):

```python
def _enrolled(row: LivenessSessionRow) -> bool:
    return row.consumed_at is not None and row.failure_reason is None


def _result_response(row: LivenessSessionRow) -> LivenessResultResponse:
    status = "passed" if row.status == "consumed" else row.status
    if status not in ("passed", "failed"):
        raise ServiceError(
            502,
            "liveness_result_inconsistent",
            f"Session finalised with unexpected status {row.status!r}.",
            retryable=True,
        )
    reason = (
        "quality_rejected" if row.failure_reason == QUALITY_REJECTED_REASON else None
    )
    return LivenessResultResponse(
        status=status, confidence=row.confidence, enrolled=_enrolled(row), reason=reason
    )
```

Add the `face_index` dependency to `post_liveness_result`'s signature:

```python
    face_index: FaceIndex = Depends(get_face_index),
```

Replace the tail of the passed branch — everything after the upload `try/except` block (the current `finalize_result(status="passed", ...)` call and its `return _finish(...)`) — with:

```python
    # Liveness passed and the frames are persisted. Index the ReferenceImage —
    # the bytes already in memory from GetFaceLivenessSessionResults; there is
    # no S3 client to re-fetch with, by design (CLAUDE.md §3.3).
    source_object_uri = _strip_query(body.reference_put_url)
    try:
        indexed = await face_index.index_face(
            collection_id=cfg.rekognition_collection_id,
            external_image_id=str(row.user_ref),  # user_ref and NOTHING else
            image_bytes=result.reference_image,
        )
    except FaceIndexUnavailable:
        # Write nothing, consume nothing: the proxy retries the whole result
        # call with the same Idempotency-Key (step-4 brief, transient failure).
        raise ServiceError(
            503,
            "face_index_unavailable",
            "Face indexing is temporarily unavailable; retry with the same"
            " Idempotency-Key.",
            retryable=True,
        ) from None

    if isinstance(indexed, IndexRejected):
        # Liveness passed; the HIGH quality filter rejected the frame. Consume
        # the session anyway — leaving it unconsumed would 409-lock the user
        # out of a fresh attempt — but write NO enrolment.
        log.info(
            "liveness.enrolment_quality_rejected",
            session_id=str(sid),
            reasons=list(indexed.reasons),
        )
        rejected = await store.finalize_quality_rejected(
            sid,
            confidence=result.confidence,
            reference_image_uri=source_object_uri,
            audit_image_uris=tuple(stored_audit_uris),
        )
        if rejected is None:
            return await _completed_replay(store, sid, idempotency_key)
        return _finish(rejected, cfg)

    outcome = await store.finalize_enrolled(
        sid,
        confidence=result.confidence,
        reference_image_uri=source_object_uri,
        audit_image_uris=tuple(stored_audit_uris),
        enrolment=NewEnrolment(
            user_ref=UserRef(row.user_ref),
            collection_id=cfg.rekognition_collection_id,
            external_face_id=indexed.face_id,
            quality_score=indexed.quality_score,
            model_id=indexed.model_id,
            source_object_uri=source_object_uri,
        ),
    )
    if outcome is None:
        # A concurrent result call finalized first — the face just indexed is
        # a duplicate. Remove it so the collection keeps exactly one face per
        # active enrolment (step-4 done-when), then replay/410 as appropriate.
        await face_index.delete_faces(cfg.rekognition_collection_id, (indexed.face_id,))
        return await _completed_replay(store, sid, idempotency_key)

    final, enrolment = outcome
    log.info(
        "liveness.enrolled",
        session_id=str(sid),
        external_face_id=enrolment.external_face_id,
        model_id=enrolment.model_id,
        quality_score=enrolment.quality_score,
    )
    return _finish(final, cfg)
```

Add the helper (below `_finish`):

```python
async def _completed_replay(
    store: LivenessStore, sid: SessionId, idempotency_key: str
) -> LivenessResultResponse:
    """Race loser's exit: someone else finalized this session mid-flight."""
    row = await store.get_session(sid)
    if row is not None and row.result_idempotency_key == idempotency_key:
        return _result_response(row)
    raise ServiceError(
        410,
        "liveness_consumed",
        "This liveness session has already been consumed.",
        retryable=False,
    )
```

Update `get_liveness_session` to report enrolment:

```python
    return LivenessStatusResponse(
        status=_effective_status(row), confidence=row.confidence, enrolled=_enrolled(row)
    )
```

Note: the early-return replay path (`row.result_idempotency_key == idempotency_key` → `_result_response(row)`) now handles consumed rows correctly because `_result_response` maps `consumed` → `passed` and derives `enrolled`/`reason`. The `if row.consumed_at is not None: 410` guard sits BEFORE the completed_at replay check in the current code — reorder so the **same-key replay check runs first** (a consumed session always has `completed_at` set; the current order would 410 a legitimate same-key retry):

```python
    if row.completed_at is not None:
        if row.result_idempotency_key == idempotency_key:
            return _result_response(row)  # idempotent replay of the same request
        raise ServiceError(
            410,
            "liveness_consumed",
            "This liveness session already has a recorded result.",
            retryable=False,
        )
    if row.consumed_at is not None:
        raise ServiceError(
            410,
            "liveness_consumed",
            "This liveness session has already been consumed.",
            retryable=False,
        )
```

- [ ] **Step 4: Run all route tests, mypy, ruff**

Run: `python -m pytest tests/test_liveness_routes.py -q && python -m mypy && python -m ruff check src tests`
Expected: ALL PASS (including the updated step-3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/imageshield/http/models.py src/imageshield/http/routes/liveness.py tests/test_liveness_routes.py
git commit -m "Step 4: index ReferenceImage on pass — enrolment, quality rejection, transient 503

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: `DELETE /v1/enrolments/{user_ref}` — DeleteFaces → verify → tombstone

Order is the invariant (CLAUDE.md §4 #7): remove from the collection, VERIFY absence with ListFaces, and only then tombstone. Any failure before the tombstone aborts — a crash mid-way leaves rows still `active` pointing at already-deleted faces, which a retry handles (DeleteFaces on absent FaceIds is a no-op); it never leaves a searchable face with no record.

**Files:**
- Modify: `src/imageshield/enrolment/store.py` (add `PostgresEnrolmentStore`)
- Create: `src/imageshield/http/routes/enrolments.py`
- Modify: `src/imageshield/http/app.py` (wire `enrolment_store`, include router)
- Test: `tests/test_enrolment_routes.py`, `tests/test_enrolment_store.py`

**Interfaces:**
- Consumes: `EnrolmentStore` protocol (Task 2), `FaceIndex.delete_faces` / `list_face_ids`, `EnrolmentRow`.
- Produces: `DELETE /v1/enrolments/{user_ref}` → `204` (idempotent — 204 also when nothing is active); `503 face_index_unavailable` when Rekognition errs; `502 face_deletion_unverified` when ListFaces still sees a face.

- [ ] **Step 1: Write the failing route tests**

Create `tests/test_enrolment_routes.py`:

```python
"""DELETE /v1/enrolments/{user_ref} — DeleteFaces -> verify -> tombstone.

Nothing calls this in v1. It exists because the old system called DeleteFaces
nowhere under a comment claiming BIPA compliance (step-4 brief) — a
face-indexing system without a deletion path accumulates vectors it can never
remove. The load-bearing property: the tombstone NEVER lands while a face is
still searchable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from imageshield.enrolment.models import EnrolmentRow, FaceIndexUnavailable
from imageshield.http.app import create_app
from tests.conftest import SERVICE_TOKEN, make_config
from tests.test_liveness_routes import FakeFaceIndex

AUTH = {"X-Service-Token": SERVICE_TOKEN}


def make_enrolment(**overrides: Any) -> EnrolmentRow:
    defaults: dict[str, Any] = {
        "enrolment_id": uuid4(),
        "session_id": uuid4(),
        "user_ref": uuid4(),
        "collection_id": "identity-v1",
        "external_face_id": f"face-{uuid4()}",
        "quality_score": 99.0,
        "model_id": "rekognition:7.0",
        "source_object_uri": "https://proxy-s3.example/ref.jpg",
        "status": "active",
        "created_at": datetime.now(UTC),
        "deleted_at": None,
    }
    defaults.update(overrides)
    return EnrolmentRow(**defaults)


class FakeEnrolmentStore:
    def __init__(self) -> None:
        self.rows: list[EnrolmentRow] = []
        self.tombstoned: list[UUID] = []
        self.fail_tombstone = False

    async def get_active_enrolments(self, user_ref: UUID) -> tuple[EnrolmentRow, ...]:
        return tuple(
            r for r in self.rows if r.user_ref == user_ref and r.status == "active"
        )

    async def tombstone_enrolments(self, user_ref: UUID) -> int:
        if self.fail_tombstone:
            raise RuntimeError("simulated crash between DeleteFaces and tombstone")
        active = [r for r in self.rows if r.user_ref == user_ref and r.status == "active"]
        self.rows = [r for r in self.rows if r not in active]
        self.tombstoned.append(user_ref)
        return len(active)


class Harness:
    def __init__(self) -> None:
        self.store = FakeEnrolmentStore()
        self.face_index = FakeFaceIndex()
        app = create_app(config=make_config())
        app.state.enrolment_store = self.store
        app.state.face_index = self.face_index
        self.client = TestClient(app)

    def enrol(self, user_ref: UUID) -> EnrolmentRow:
        row = make_enrolment(user_ref=user_ref)
        self.store.rows.append(row)
        self.face_index.faces[row.external_face_id] = (row.collection_id, str(user_ref))
        return row

    def delete(self, user_ref: UUID) -> Any:
        return self.client.delete(f"/v1/enrolments/{user_ref}", headers=AUTH)


def test_delete_removes_faces_verifies_then_tombstones() -> None:
    h = Harness()
    user_ref = uuid4()
    row = h.enrol(user_ref)

    response = h.delete(user_ref)

    assert response.status_code == 204
    assert row.external_face_id not in h.face_index.faces  # gone from the collection
    assert h.store.tombstoned == [user_ref]
    assert h.store.rows == []  # all rows tombstoned


def test_delete_with_no_active_enrolments_is_idempotent_204() -> None:
    h = Harness()

    response = h.delete(uuid4())

    assert response.status_code == 204
    assert h.store.tombstoned == []


def test_rekognition_failure_aborts_before_tombstone() -> None:
    h = Harness()
    user_ref = uuid4()
    h.enrol(user_ref)

    async def failing_delete(collection_id: str, face_ids: tuple[str, ...]) -> None:
        raise FaceIndexUnavailable("DeleteFaces failed with ThrottlingException")

    h.face_index.delete_faces = failing_delete  # type: ignore[method-assign]

    response = h.delete(user_ref)

    assert response.status_code == 503
    assert response.json()["error"]["retryable"] is True
    assert h.store.tombstoned == []  # record still points at the face


def test_unverified_deletion_aborts_before_tombstone() -> None:
    h = Harness()
    user_ref = uuid4()
    row = h.enrol(user_ref)

    async def noop_delete(collection_id: str, face_ids: tuple[str, ...]) -> None:
        return None  # face stays in the collection

    h.face_index.delete_faces = noop_delete  # type: ignore[method-assign]

    response = h.delete(user_ref)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "face_deletion_unverified"
    assert h.store.tombstoned == []
    assert row.external_face_id in h.face_index.faces


def test_crash_between_deletefaces_and_tombstone_leaves_no_searchable_face() -> None:
    """Step-4 done-when: kill the process between DeleteFaces and the
    tombstone — the face must already be gone. The row stays active (record
    without face — recoverable by retry), never the reverse (face without
    record — unrecoverable without a full collection audit)."""
    h = Harness()
    user_ref = uuid4()
    row = h.enrol(user_ref)
    h.store.fail_tombstone = True

    response = h.delete(user_ref)

    assert response.status_code == 500
    assert row.external_face_id not in h.face_index.faces  # NOT searchable
    assert h.store.rows[0].status == "active"  # record survives for the retry

    # And the retry completes: DeleteFaces on absent faces is a no-op.
    h.store.fail_tombstone = False
    assert h.delete(user_ref).status_code == 204
    assert h.store.tombstoned == [user_ref]


def test_delete_requires_service_token() -> None:
    h = Harness()

    response = h.client.delete(f"/v1/enrolments/{uuid4()}")

    assert response.status_code == 401
```

- [ ] **Step 2: Write the failing store tests**

Create `tests/test_enrolment_store.py`:

```python
"""PostgresEnrolmentStore against real Postgres (same convention as
tests/test_liveness_store.py: own down --all + up arrange step)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from imageshield.db.connection import make_async_pool
from imageshield.enrolment.models import NewEnrolment
from imageshield.enrolment.store import PostgresEnrolmentStore
from imageshield.liveness.models import LivenessSessionRow
from imageshield.liveness.store import PostgresLivenessStore
from tests.db import run_migrate


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


@pytest.fixture
async def stores(
    migrated_db: str,
) -> AsyncIterator[tuple[PostgresLivenessStore, PostgresEnrolmentStore]]:
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresLivenessStore(pool), PostgresEnrolmentStore(pool)
    finally:
        await pool.close()


async def _enrol_user(
    liveness: PostgresLivenessStore, user_ref: object
) -> str:
    """Create + pass + enrol one session; return the external_face_id."""
    row = await liveness.create_session(
        user_ref=user_ref,  # type: ignore[arg-type]
        provider_session_id=f"prov-{uuid4()}",
        ttl_seconds=600,
        max_attempts_24h=5,
    )
    assert isinstance(row, LivenessSessionRow), f"expected a row, got {row}"
    face_id = f"face-{uuid4()}"
    outcome = await liveness.finalize_enrolled(
        row.session_id,
        confidence=98.0,
        reference_image_uri="https://proxy-s3.example/ref.jpg",
        audit_image_uris=(),
        enrolment=NewEnrolment(
            user_ref=user_ref,  # type: ignore[arg-type]
            collection_id="identity-v1",
            external_face_id=face_id,
            quality_score=99.0,
            model_id="rekognition:7.0",
            source_object_uri="https://proxy-s3.example/ref.jpg",
        ),
    )
    assert outcome is not None
    return face_id


async def test_get_active_returns_only_this_users_active_rows(
    stores: tuple[PostgresLivenessStore, PostgresEnrolmentStore],
) -> None:
    liveness, enrolments = stores
    user_ref, other = uuid4(), uuid4()
    face_id = await _enrol_user(liveness, user_ref)
    await _enrol_user(liveness, other)

    active = await enrolments.get_active_enrolments(user_ref)  # type: ignore[arg-type]

    assert [e.external_face_id for e in active] == [face_id]
    assert all(e.status == "active" for e in active)


async def test_tombstone_flips_status_and_is_idempotent(
    stores: tuple[PostgresLivenessStore, PostgresEnrolmentStore],
) -> None:
    liveness, enrolments = stores
    user_ref = uuid4()
    await _enrol_user(liveness, user_ref)

    first = await enrolments.tombstone_enrolments(user_ref)  # type: ignore[arg-type]
    second = await enrolments.tombstone_enrolments(user_ref)  # type: ignore[arg-type]

    assert first == 1
    assert second == 0  # nothing active left: idempotent
    assert await enrolments.get_active_enrolments(user_ref) == ()  # type: ignore[arg-type]
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest tests/test_enrolment_routes.py tests/test_enrolment_store.py -q`
Expected: FAIL — no `PostgresEnrolmentStore`, 404 on the route.

- [ ] **Step 4: Implement**

Append to `src/imageshield/enrolment/store.py`:

```python
from typing import Any

from psycopg_pool import AsyncConnectionPool

_ENROLMENT_COLUMNS = (
    "enrolment_id, session_id, user_ref, collection_id, external_face_id,"
    " quality_score, model_id, source_object_uri, status, created_at, deleted_at"
)

_ACTIVE_SQL = f"""
    SELECT {_ENROLMENT_COLUMNS} FROM enrolments
    WHERE user_ref = %(user_ref)s AND status = 'active'
    ORDER BY created_at
"""

# Soft delete (CLAUDE.md §5): never DELETE — biometric enrolments are
# expensive to recreate and the row is the only record the face ever existed.
_TOMBSTONE_SQL = """
    UPDATE enrolments
    SET status = 'deleted', deleted_at = now()
    WHERE user_ref = %(user_ref)s AND status = 'active'
"""


class PostgresEnrolmentStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def get_active_enrolments(self, user_ref: UserRef) -> tuple[EnrolmentRow, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_ACTIVE_SQL, {"user_ref": user_ref})
            records = await cur.fetchall()
        return tuple(to_enrolment_row(record) for record in records)

    async def tombstone_enrolments(self, user_ref: UserRef) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_TOMBSTONE_SQL, {"user_ref": user_ref})
            return cur.rowcount
```

(`to_enrolment_row` already lives in this module from Task 3.)

Create `src/imageshield/http/routes/enrolments.py`:

```python
"""Enrolment deletion (CLAUDE.md §8 step 4, INVARIANTS #7).

Order is the invariant: DeleteFaces, VERIFY absence via ListFaces, and only
then tombstone. A crash mid-way leaves an active row pointing at deleted
faces — the retry completes it. It never leaves a searchable face with no
record pointing at it, which is unrecoverable without a full collection audit.

Nothing calls this in v1. It exists because the old system called DeleteFaces
nowhere (under a comment asserting BIPA compliance), so every face ever
enrolled there is still searchable, including deleted accounts'.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Response

from imageshield.enrolment.faceindex import FaceIndex
from imageshield.enrolment.models import FaceIndexUnavailable
from imageshield.enrolment.store import EnrolmentStore
from imageshield.http.auth import require_service_token
from imageshield.http.deps import get_enrolment_store, get_face_index
from imageshield.http.errors import ServiceError
from imageshield.types import UserRef

log = structlog.get_logger("imageshield.enrolment")

router = APIRouter(prefix="/v1/enrolments", dependencies=[Depends(require_service_token)])


@router.delete("/{user_ref}", status_code=204)
async def delete_enrolments(
    user_ref: UUID,
    store: EnrolmentStore = Depends(get_enrolment_store),
    face_index: FaceIndex = Depends(get_face_index),
) -> Response:
    ref = UserRef(user_ref)
    active = await store.get_active_enrolments(ref)
    if not active:
        # Idempotent: nothing active means nothing searchable — the goal state.
        return Response(status_code=204)

    by_collection: dict[str, list[str]] = {}
    for enrolment in active:
        by_collection.setdefault(enrolment.collection_id, []).append(
            enrolment.external_face_id
        )

    try:
        for collection_id, face_ids in by_collection.items():
            await face_index.delete_faces(collection_id, tuple(face_ids))
            remaining = await face_index.list_face_ids(collection_id, tuple(face_ids))
            if remaining:
                # ABORT before the tombstone: a tombstoned row with a live
                # face is the unrecoverable state (INVARIANTS #7).
                log.error(
                    "enrolment.delete_unverified",
                    user_ref=str(ref),
                    collection_id=collection_id,
                    remaining=len(remaining),
                )
                raise ServiceError(
                    502,
                    "face_deletion_unverified",
                    "DeleteFaces completed but ListFaces still returns faces;"
                    " nothing was tombstoned. Retry.",
                    retryable=True,
                )
    except FaceIndexUnavailable:
        raise ServiceError(
            503,
            "face_index_unavailable",
            "Face deletion is temporarily unavailable; nothing was tombstoned."
            " Retry.",
            retryable=True,
        ) from None

    tombstoned = await store.tombstone_enrolments(ref)
    log.info("enrolment.deleted", user_ref=str(ref), enrolments=tombstoned)
    return Response(status_code=204)
```

`src/imageshield/http/app.py`: add `from imageshield.enrolment.store import PostgresEnrolmentStore` and `from imageshield.http.routes.enrolments import router as enrolments_router`; wire `app.state.enrolment_store` in the lifespan (guarded, next to `face_index`); `app.include_router(enrolments_router)`.

- [ ] **Step 5: Run everything for this task**

Run: `python -m pytest tests/test_enrolment_routes.py tests/test_enrolment_store.py tests/test_liveness_store.py -q && python -m mypy && python -m ruff check src tests`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add src/imageshield/enrolment/store.py src/imageshield/liveness/store.py src/imageshield/http/routes/enrolments.py src/imageshield/http/app.py tests/test_enrolment_routes.py tests/test_enrolment_store.py
git commit -m "DELETE /v1/enrolments/{user_ref}: DeleteFaces -> verify -> tombstone

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Boundary grep test, docs, full verification

**Files:**
- Create: `tests/test_boundaries.py`
- Modify: `PROXY_INTEGRATION.md` (§4 table)
- Modify: `SCHEMA.md` (migration 0003 note)
- Test: full suite

- [ ] **Step 1: Write the boundary test (it should pass immediately — it is a tripwire, not TDD)**

```python
"""Tripwires for the identity boundary (CLAUDE.md §4 #1, step-4 done-when).

Step 9 adds these as CI greps; having them as tests too means a violation
fails BEFORE a PR exists. Permanent — never delete.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# The fragmentation bug's fingerprints: any face-search call in this codebase
# means identity is about to come from a similarity score (INVARIANTS #1).
FORBIDDEN_SEARCH = re.compile(
    r"search_faces_by_image|SearchFacesByImage|search_users_by_image|search_faces\b|SearchFaces\b"
)

# No S3 client, ever (CLAUDE.md §3.3): presigned URLs via httpx are the only
# path to bytes.
FORBIDDEN_S3 = re.compile(r"""boto3\.client\(\s*["']s3["']|boto3\.resource\(\s*["']s3["']""")


def _source_files() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    assert files, "src/ scan found nothing — path wrong?"
    return files


def test_no_face_search_anywhere_in_src() -> None:
    offenders = [
        str(path)
        for path in _source_files()
        if FORBIDDEN_SEARCH.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_no_s3_client_anywhere_in_src() -> None:
    offenders = [
        str(path)
        for path in _source_files()
        if FORBIDDEN_S3.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
```

Run: `python -m pytest tests/test_boundaries.py -q` → PASS. Also run the brief's literal grep and confirm empty:
`grep -rE "search_faces_by_image|SearchFacesByImage|search_users_by_image" src/`

- [ ] **Step 2: Update the docs (same PR as the code — CLAUDE.md §10)**

`PROXY_INTEGRATION.md` §4 table:
- `POST /v1/liveness/{sid}/result` row: replace "`enrolled` is always `false` until step 4" with "`enrolled` true once IndexFaces succeeds (step 4); `passed` + `enrolled:false` + `reason:'quality_rejected'` means start a fresh session".
- `GET /v1/liveness/{sid}` row: drop "from step 4" phrasing (it is now current behaviour).
- Replace the `DELETE /v1/users/{id}` row with `DELETE /v1/enrolments/{user_ref}` — "`DeleteFaces` → verify → tombstone (built, step 4). Idempotent; nothing calls it in v1." Add one line noting the divergence: full user deletion (`/v1/users/{id}`) remains specified-not-built; enrolment deletion is the piece this repo owns and it exists now.
- In §3 or §4, one line: enrolment completion also emits Postgres `NOTIFY enrolment_complete` with the `session_id` as payload (wake-up only; read authoritative state from the table).

`SCHEMA.md`: in the enrolments/liveness section, note migration 0003: `enrolments.session_status` (CHECK `'consumed'`) + composite FK `(session_id, session_status) → liveness_sessions(session_id, status)` — the DB-level form of INVARIANTS #2; and that `UNIQUE (session_id, status)` exists on `liveness_sessions` to support it.

- [ ] **Step 3: Full verification (superpowers:verification-before-completion)**

Run, and read the output before claiming success:

```bash
python -m pytest -q
python -m mypy
python -m ruff check src tests
python scripts/migrate.py up --dry-run  # against the local compose DB if running
grep -rE "search_faces_by_image|SearchFacesByImage|search_users_by_image" src/ || echo CLEAN
```

Expected: full suite PASS (DB tests skip if compose Postgres is down — start it: `docker compose -f docker-compose.local.yml up -d` so they RUN), mypy clean, ruff clean, grep CLEAN.

- [ ] **Step 4: Commit**

```bash
git add tests/test_boundaries.py PROXY_INTEGRATION.md SCHEMA.md
git commit -m "Step 4: boundary tripwire tests + contract docs for enrolment and deletion

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Done-when mapping (step-4 brief → where proven)

| Done-when | Proven by |
|---|---|
| Two lookalikes → two distinct identities (PERMANENT) | `test_lookalike_users_enrol_as_two_distinct_identities` (Task 4) |
| Failed session cannot enrol — **DB level** | `test_failed_session_cannot_enrol` + FK tests (Task 1) |
| Replay of consumed session → 410, no double-index | `test_different_key_after_enrolment_is_410`, `test_same_key_replay_after_enrolment_does_not_double_index` (Task 4) |
| Quality rejection consumes, no enrolment, distinguishable reason | `test_quality_rejection_consumes_without_enrolment` (Task 4) |
| DELETE removes face, verified by ListFaces | `test_delete_removes_faces_verifies_then_tombstones` (Task 5) |
| Kill between DeleteFaces and tombstone → no searchable face | `test_crash_between_deletefaces_and_tombstone_leaves_no_searchable_face` (Task 5) |
| grep for search-by-face returns nothing | `test_no_face_search_anywhere_in_src` + manual grep (Task 6) |
| Collection face count == active enrolments rows | Race compensation in Task 4 (`outcome is None` → `delete_faces`); real-AWS spot-check available via `devtools/check_collection.py` |

## Deliberate notes for the final report

- `identity:index` stays provisioned but **unused**: indexing is synchronous with retry-by-caller (503 + same `Idempotency-Key`). Deliberate simplification of ARCHITECTURE.md — do not invent async work.
- Endpoint is `DELETE /v1/enrolments/{user_ref}` per the step-4 brief and NEAR-TERM-BUILD 1.3; PROXY_INTEGRATION.md's older `DELETE /v1/users/{id}` row updated to match. Full user deletion remains specified-not-built.
- `model_id` is `rekognition:<FaceModelVersion>` from the IndexFaces response (INVARIANTS #4); `quality_score` is `FaceRecords[0].Face.Confidence`.
- Every Rekognition `ClientError` on IndexFaces maps to 503-retryable. A permanently failing call surfaces as repeated 503s with the AWS error code logged — acceptable for v1 (proxy retries are bounded at 3).
- Real-device E2E (real Rekognition, real lookalike photos) remains the step-4 exit gate that unit tests cannot replace — CLAUDE.md §8: "Do not start step 5 before step 4 is verified end-to-end on a real device."
