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
        self.client = TestClient(app, raise_server_exceptions=False)

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
