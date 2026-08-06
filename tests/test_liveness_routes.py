"""Liveness session lifecycle — route behaviour (CLAUDE.md §8 step 3).

These tests exercise the three endpoints through the FastAPI app with
in-memory fakes standing in for the three I/O ports (store, provider,
uploader), following the repo convention that ``TestClient`` never runs the
lifespan (tests/conftest.py). The real Postgres store is tested separately in
``tests/test_liveness_store.py``; the real Rekognition provider is exercised
only by the devtools harness and the real-device E2E.

The behaviours pinned here are the step-3 "done when" list:

- 201 create with the contract shape; 409 passed-but-unconsumed; 429 over the
  24h attempt cap; 400 on any ``Idempotency-Key`` (create is NOT idempotent —
  a blind retry burns a provider session and an attempt);
- result call persists the ReferenceImage through the presigned PUT (the one
  thing this step must not get wrong), stores ``reference_image_uri`` with
  the presigned query string stripped (the signature is a credential);
- threshold from config, never inline (CLAUDE.md §4 1b);
- 410 for consumed and for expired sessions — never a 500;
- ``Idempotency-Key`` required on the result call; same-key replay returns
  the stored outcome without re-calling the provider; different-key replay of
  a completed session is 410 (single use).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from imageshield.enrolment.models import (
    QUALITY_REJECTED_REASON,
    EnrolmentRow,
    FaceIndexUnavailable,
    IndexedFace,
    IndexRejected,
    NewEnrolment,
)
from imageshield.http.app import create_app
from imageshield.liveness.models import (
    CreateRejection,
    LivenessSessionRow,
    ProviderResult,
    UploadError,
)
from tests.conftest import SERVICE_TOKEN, make_config

AUTH = {"X-Service-Token": SERVICE_TOKEN}


def _now() -> datetime:
    return datetime.now(UTC)


def make_row(**overrides: Any) -> LivenessSessionRow:
    defaults: dict[str, Any] = {
        "session_id": uuid4(),
        "user_ref": uuid4(),
        "provider_session_id": f"prov-{uuid4()}",
        "status": "created",
        "confidence": None,
        "failure_reason": None,
        "attempt_number": 1,
        "reference_image_uri": None,
        "audit_image_uris": None,
        "created_at": _now(),
        "completed_at": None,
        "expires_at": _now() + timedelta(seconds=600),
        "consumed_at": None,
        "result_idempotency_key": None,
    }
    defaults.update(overrides)
    return LivenessSessionRow(**defaults)


class FakeLivenessStore:
    """In-memory mirror of the LivenessStore protocol semantics."""

    def __init__(self) -> None:
        self.rows: dict[UUID, LivenessSessionRow] = {}
        self.enrolments: dict[UUID, EnrolmentRow] = {}  # keyed by session_id
        self.notifies: list[str] = []

    def add(self, row: LivenessSessionRow) -> LivenessSessionRow:
        self.rows[row.session_id] = row
        return row

    def set(self, session_id: UUID, **changes: Any) -> LivenessSessionRow:
        self.rows[session_id] = replace(self.rows[session_id], **changes)
        return self.rows[session_id]

    def _rejection(self, user_ref: UUID, max_attempts_24h: int) -> CreateRejection | None:
        mine = [r for r in self.rows.values() if r.user_ref == user_ref]
        if any(r.status == "passed" and r.consumed_at is None for r in mine):
            return CreateRejection.PASSED_UNCONSUMED
        cutoff = _now() - timedelta(hours=24)
        if sum(1 for r in mine if r.created_at > cutoff) >= max_attempts_24h:
            return CreateRejection.ATTEMPTS_EXCEEDED
        return None

    async def check_create_allowed(
        self, user_ref: UUID, *, max_attempts_24h: int
    ) -> CreateRejection | None:
        return self._rejection(user_ref, max_attempts_24h)

    async def create_session(
        self,
        *,
        user_ref: UUID,
        provider_session_id: str,
        ttl_seconds: int,
        max_attempts_24h: int,
    ) -> LivenessSessionRow | CreateRejection:
        rejection = self._rejection(user_ref, max_attempts_24h)
        if rejection is not None:
            return rejection
        attempts = sum(1 for r in self.rows.values() if r.user_ref == user_ref)
        row = make_row(
            user_ref=user_ref,
            provider_session_id=provider_session_id,
            attempt_number=attempts + 1,
            expires_at=_now() + timedelta(seconds=ttl_seconds),
        )
        return self.add(row)

    async def get_session(self, session_id: UUID) -> LivenessSessionRow | None:
        return self.rows.get(session_id)

    async def claim_result(self, session_id: UUID, idempotency_key: str) -> None:
        if self.rows[session_id].completed_at is None:
            self.set(session_id, result_idempotency_key=idempotency_key)

    async def finalize_result(
        self,
        session_id: UUID,
        *,
        status: str,
        confidence: float | None,
        failure_reason: str | None,
        reference_image_uri: str | None,
        audit_image_uris: tuple[str, ...] | None,
    ) -> LivenessSessionRow:
        return self.set(
            session_id,
            status=status,
            confidence=confidence,
            failure_reason=failure_reason,
            reference_image_uri=reference_image_uri,
            audit_image_uris=audit_image_uris,
            completed_at=_now(),
        )

    async def mark_expired(self, session_id: UUID) -> LivenessSessionRow:
        if self.rows[session_id].completed_at is None:
            return self.set(session_id, status="expired")
        return self.rows[session_id]

    async def finalize_enrolled(
        self,
        session_id: UUID,
        *,
        confidence: float | None,
        reference_image_uri: str,
        audit_image_uris: tuple[str, ...],
        enrolment: NewEnrolment,
    ) -> tuple[LivenessSessionRow, EnrolmentRow] | None:
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


class FakeLivenessProvider:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.results: dict[str, ProviderResult | Exception] = {}
        self.get_calls: list[str] = []

    async def create_session(self) -> str:
        provider_session_id = f"prov-{uuid4()}"
        self.created.append(provider_session_id)
        return provider_session_id

    async def get_result(self, provider_session_id: str) -> ProviderResult:
        self.get_calls.append(provider_session_id)
        result = self.results[provider_session_id]
        if isinstance(result, Exception):
            raise result
        return result


class FakeUploader:
    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes, str]] = []
        self.fail = False

    async def put(self, url: str, data: bytes, *, content_type: str) -> None:
        if self.fail:
            raise UploadError("presigned PUT returned 403")
        self.puts.append((url, data, content_type))


class FakeFaceIndex:
    """In-memory Rekognition collection. Every accepted index mints a fresh
    FaceId — exactly like the real thing, which is why two lookalikes can
    never collapse into one identity here: nothing ever searches (this class,
    like the production FaceIndex protocol, has no search method at all)."""

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


class Harness:
    def __init__(self, **config_overrides: Any) -> None:
        self.store = FakeLivenessStore()
        self.provider = FakeLivenessProvider()
        self.uploader = FakeUploader()
        self.face_index = FakeFaceIndex()
        app = create_app(config=make_config(**config_overrides))
        app.state.liveness_store = self.store
        app.state.liveness_provider = self.provider
        app.state.object_uploader = self.uploader
        app.state.face_index = self.face_index
        self.client = TestClient(app)

    # -- convenience -------------------------------------------------------

    def create(self, user_ref: UUID | str, headers: dict[str, str] | None = None) -> Any:
        return self.client.post(
            "/v1/liveness/sessions",
            json={"user_ref": str(user_ref)},
            headers={**AUTH, **(headers or {})},
        )

    def result(
        self,
        session_id: UUID,
        body: dict[str, Any] | None = None,
        *,
        key: str | None = "idem-key-1",
        headers: dict[str, str] | None = None,
    ) -> Any:
        merged = {**AUTH, **(headers or {})}
        if key is not None:
            merged["Idempotency-Key"] = key
        if body is None:
            body = {
                "reference_put_url": "https://proxy-s3.example/ref.jpg?X-Amz-Signature=abc",
                "audit_put_urls": [
                    "https://proxy-s3.example/audit-0.jpg?X-Amz-Signature=def",
                    "https://proxy-s3.example/audit-1.jpg?X-Amz-Signature=ghi",
                ],
            }
        return self.client.post(f"/v1/liveness/{session_id}/result", json=body, headers=merged)

    def passed_provider_result(
        self, provider_session_id: str, *, confidence: float = 99.5, audit_count: int = 2
    ) -> None:
        self.provider.results[provider_session_id] = ProviderResult(
            status="succeeded",
            confidence=confidence,
            reference_image=b"reference-jpeg-bytes",
            audit_images=tuple(f"audit-{i}".encode() for i in range(audit_count)),
        )


def error_body(response: Any) -> dict[str, Any]:
    body = response.json()
    assert set(body) == {"error"}, f"expected the error envelope, got {body}"
    envelope: dict[str, Any] = body["error"]
    assert set(envelope) == {"code", "message", "retryable", "request_id"}
    return envelope


# ── POST /v1/liveness/sessions ───────────────────────────────────────────────


def test_create_session_returns_201_with_contract_shape() -> None:
    h = Harness()
    response = h.create(uuid4())

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"session_id", "provider_session_id", "region", "expires_at"}
    assert body["provider_session_id"] == h.provider.created[0]
    assert body["region"] == "ap-south-1"  # make_config's aws_region — from config, not hardcoded
    UUID(body["session_id"])  # must parse
    expires_at = datetime.fromisoformat(body["expires_at"])
    delta = (expires_at - _now()).total_seconds()
    assert 590 < delta <= 610, "expires_at must be ~LIVENESS_SESSION_TTL_SECONDS from now"


def test_create_session_persists_row_with_created_status() -> None:
    h = Harness()
    user_ref = uuid4()
    response = h.create(user_ref)

    row = h.store.rows[UUID(response.json()["session_id"])]
    assert row.user_ref == user_ref
    assert row.status == "created"
    assert row.provider_session_id == response.json()["provider_session_id"]


def test_create_conflicts_when_passed_unconsumed_session_exists() -> None:
    h = Harness()
    user_ref = uuid4()
    h.store.add(make_row(user_ref=user_ref, status="passed", completed_at=_now()))

    response = h.create(user_ref)

    assert response.status_code == 409
    assert error_body(response)["code"] == "liveness_already_passed"
    assert h.provider.created == [], "a rejected create must not burn a provider session"


def test_create_429_when_24h_attempts_exhausted() -> None:
    h = Harness(liveness_max_attempts_24h=3)
    user_ref = uuid4()
    for _ in range(3):
        h.store.add(make_row(user_ref=user_ref))

    response = h.create(user_ref)

    assert response.status_code == 429
    assert error_body(response)["code"] == "liveness_attempts_exceeded"
    assert h.provider.created == [], "a rate-limited create must not burn a provider session"


def test_create_attempts_older_than_24h_do_not_count() -> None:
    h = Harness(liveness_max_attempts_24h=3)
    user_ref = uuid4()
    for _ in range(3):
        h.store.add(make_row(user_ref=user_ref, created_at=_now() - timedelta(hours=25)))

    assert h.create(user_ref).status_code == 201


def test_create_rejects_idempotency_key_header_with_400() -> None:
    """NOT idempotent: it creates a provider session and burns an attempt.
    Accepting the header would let a caller assume retry safety and silently
    lock users out of enrolment."""
    h = Harness()

    response = h.create(uuid4(), headers={"Idempotency-Key": "retry-me"})

    assert response.status_code == 400
    assert error_body(response)["code"] == "idempotency_key_not_allowed"
    assert h.provider.created == []


def test_create_unknown_body_field_is_422() -> None:
    h = Harness()
    response = h.client.post(
        "/v1/liveness/sessions",
        json={"user_ref": str(uuid4()), "phone": "+15551234567"},
        headers=AUTH,
    )
    assert response.status_code == 422


def test_create_non_uuid_user_ref_is_422() -> None:
    h = Harness()
    response = h.client.post(
        "/v1/liveness/sessions", json={"user_ref": "not-a-uuid"}, headers=AUTH
    )
    assert response.status_code == 422


def test_create_requires_service_token() -> None:
    h = Harness()
    response = h.client.post("/v1/liveness/sessions", json={"user_ref": str(uuid4())})
    assert response.status_code == 401


# ── POST /v1/liveness/{session_id}/result ────────────────────────────────────


def test_result_passed_persists_reference_image_through_presigned_put() -> None:
    """The one thing this step must not get wrong: the ReferenceImage bytes
    are PUT through the presigned URL and the URI is stored — not merely read."""
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row.provider_session_id, confidence=99.5, audit_count=2)

    response = h.result(row.session_id)

    assert response.status_code == 200
    assert response.json() == {
        "status": "passed",
        "confidence": 99.5,
        "enrolled": True,  # step 4: a passed, indexed session enrols
        "reason": None,
    }

    put_urls = [url for url, _, _ in h.uploader.puts]
    assert put_urls[0].startswith("https://proxy-s3.example/ref.jpg")
    assert h.uploader.puts[0][1] == b"reference-jpeg-bytes"
    assert len(h.uploader.puts) == 3  # reference + 2 audit frames

    stored = h.store.rows[row.session_id]
    assert stored.status == "consumed"  # step 4: enrolment consumes the session
    assert stored.completed_at is not None
    assert stored.reference_image_uri == "https://proxy-s3.example/ref.jpg"
    assert stored.audit_image_uris == (
        "https://proxy-s3.example/audit-0.jpg",
        "https://proxy-s3.example/audit-1.jpg",
    )


def test_result_stored_uris_never_contain_presigned_query() -> None:
    """The query string is the signature — a credential. It must not persist."""
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row.provider_session_id)

    h.result(row.session_id)

    stored = h.store.rows[row.session_id]
    assert stored.reference_image_uri is not None
    assert "?" not in stored.reference_image_uri
    assert all("?" not in uri for uri in stored.audit_image_uris or ())


def test_result_below_threshold_fails_and_skips_puts() -> None:
    h = Harness()  # liveness_min_confidence=90 via make_config
    row = h.store.add(make_row())
    h.passed_provider_result(row.provider_session_id, confidence=85.0)

    response = h.result(row.session_id)

    assert response.status_code == 200
    assert response.json() == {
        "status": "failed",
        "confidence": 85.0,
        "enrolled": False,
        "reason": None,
    }
    assert h.uploader.puts == [], "a failed check must not persist images"
    stored = h.store.rows[row.session_id]
    assert stored.status == "failed"
    assert stored.completed_at is not None
    assert stored.failure_reason == "confidence_below_threshold"
    assert stored.reference_image_uri is None


def test_result_threshold_comes_from_config_not_a_literal() -> None:
    h = Harness(liveness_min_confidence=80.0)
    row = h.store.add(make_row())
    h.passed_provider_result(row.provider_session_id, confidence=85.0)

    response = h.result(row.session_id)

    assert response.json()["status"] == "passed"


def test_result_provider_failure_maps_to_failed() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.provider.results[row.provider_session_id] = ProviderResult(
        status="failed", confidence=12.5, reference_image=None, audit_images=()
    )

    response = h.result(row.session_id)

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert h.uploader.puts == []
    assert h.store.rows[row.session_id].failure_reason == "provider_reported_failure"


def test_result_missing_idempotency_key_is_400() -> None:
    h = Harness()
    row = h.store.add(make_row())

    response = h.result(row.session_id, key=None)

    assert response.status_code == 400
    assert error_body(response)["code"] == "idempotency_key_missing"
    assert h.provider.get_calls == []


def test_result_missing_reference_put_url_is_400() -> None:
    h = Harness()
    row = h.store.add(make_row())

    response = h.result(row.session_id, body={"audit_put_urls": ["https://x.example/a.jpg"]})

    assert response.status_code == 400
    assert error_body(response)["code"] == "presigned_urls_missing"


def test_result_missing_audit_put_urls_is_400() -> None:
    h = Harness()
    row = h.store.add(make_row())

    response = h.result(row.session_id, body={"reference_put_url": "https://x.example/r.jpg"})

    assert response.status_code == 400
    assert error_body(response)["code"] == "presigned_urls_missing"


def test_result_non_http_presigned_url_is_400() -> None:
    h = Harness()
    row = h.store.add(make_row())

    response = h.result(
        row.session_id,
        body={"reference_put_url": "file:///etc/passwd", "audit_put_urls": []},
    )

    assert response.status_code == 400
    assert error_body(response)["code"] == "presigned_urls_invalid"


def test_result_unknown_session_is_404() -> None:
    h = Harness()
    response = h.result(uuid4())
    assert response.status_code == 404
    assert error_body(response)["code"] == "session_not_found"


def test_result_consumed_session_is_410() -> None:
    h = Harness()
    row = h.store.add(
        make_row(status="passed", completed_at=_now(), consumed_at=_now(), confidence=99.0)
    )

    response = h.result(row.session_id)

    assert response.status_code == 410
    assert error_body(response)["code"] == "liveness_consumed"
    assert h.provider.get_calls == []


def test_result_expired_session_is_410_not_500() -> None:
    h = Harness()
    row = h.store.add(make_row(expires_at=_now() - timedelta(seconds=1)))

    response = h.result(row.session_id)

    assert response.status_code == 410
    assert error_body(response)["code"] == "liveness_expired"
    assert h.store.rows[row.session_id].status == "expired"
    assert h.provider.get_calls == []


def test_result_replay_same_key_returns_stored_outcome_without_provider_call() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row.provider_session_id)

    first = h.result(row.session_id, key="idem-A")
    second = h.result(row.session_id, key="idem-A")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(h.provider.get_calls) == 1, "same-key replay must not re-call the provider"
    assert len(h.uploader.puts) == 3, "same-key replay must not re-PUT images"


def test_result_replay_with_different_key_after_completion_is_410() -> None:
    """Sessions are single-use. A different Idempotency-Key is a genuine
    replay attempt, not a retry of the same request."""
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row.provider_session_id)

    first = h.result(row.session_id, key="idem-A")
    second = h.result(row.session_id, key="idem-B")

    assert first.status_code == 200
    assert second.status_code == 410
    assert error_body(second)["code"] == "liveness_consumed"


def test_result_not_ready_is_409_and_retryable() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.provider.results[row.provider_session_id] = ProviderResult(
        status="in_progress", confidence=None, reference_image=None, audit_images=()
    )

    response = h.result(row.session_id)

    assert response.status_code == 409
    envelope = error_body(response)
    assert envelope["code"] == "liveness_result_not_ready"
    assert envelope["retryable"] is True
    assert h.store.rows[row.session_id].completed_at is None


def test_result_provider_expired_marks_session_expired_and_410() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.provider.results[row.provider_session_id] = ProviderResult(
        status="expired", confidence=None, reference_image=None, audit_images=()
    )

    response = h.result(row.session_id)

    assert response.status_code == 410
    assert error_body(response)["code"] == "liveness_expired"
    assert h.store.rows[row.session_id].status == "expired"


def test_result_upload_failure_is_502_then_same_key_retry_succeeds() -> None:
    """A failed presigned PUT must not finalize the session: the proxy retries
    with the same Idempotency-Key and the retry re-processes."""
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row.provider_session_id)

    h.uploader.fail = True
    first = h.result(row.session_id, key="idem-A")
    assert first.status_code == 502
    envelope = error_body(first)
    assert envelope["code"] == "presigned_put_failed"
    assert envelope["retryable"] is True
    assert h.store.rows[row.session_id].completed_at is None

    h.uploader.fail = False
    second = h.result(row.session_id, key="idem-A")
    assert second.status_code == 200
    assert second.json()["status"] == "passed"
    assert h.store.rows[row.session_id].status == "consumed"  # step 4: retry enrols


def test_result_uploads_pair_audit_images_with_urls() -> None:
    """Three audit frames, two URLs: PUT the pairs that exist, store exactly
    the URIs that were written."""
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row.provider_session_id, audit_count=3)

    response = h.result(row.session_id)  # default body carries 2 audit URLs

    assert response.status_code == 200
    assert len(h.uploader.puts) == 3  # 1 reference + 2 paired audit frames
    assert len(h.store.rows[row.session_id].audit_image_uris or ()) == 2


def test_result_error_envelope_carries_request_id() -> None:
    h = Harness()
    response = h.result(uuid4(), headers={"X-Request-Id": "req-test-42"})

    assert error_body(response)["request_id"] == "req-test-42"
    assert response.headers["x-request-id"] == "req-test-42"


def test_result_requires_service_token() -> None:
    h = Harness()
    row = h.store.add(make_row())
    response = h.client.post(
        f"/v1/liveness/{row.session_id}/result",
        json={"reference_put_url": "https://x.example/r.jpg", "audit_put_urls": []},
        headers={"Idempotency-Key": "idem-A"},
    )
    assert response.status_code == 401


# ── GET /v1/liveness/{session_id} ────────────────────────────────────────────


def test_get_session_returns_status_and_confidence() -> None:
    h = Harness()
    row = h.store.add(make_row(status="passed", confidence=97.25, completed_at=_now()))

    response = h.client.get(f"/v1/liveness/{row.session_id}", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"status": "passed", "confidence": 97.25, "enrolled": False}


def test_get_unknown_session_is_404() -> None:
    h = Harness()
    response = h.client.get(f"/v1/liveness/{uuid4()}", headers=AUTH)
    assert response.status_code == 404
    assert error_body(response)["code"] == "session_not_found"


def test_get_expired_uncompleted_session_reports_expired() -> None:
    h = Harness()
    row = h.store.add(make_row(expires_at=_now() - timedelta(minutes=1)))

    response = h.client.get(f"/v1/liveness/{row.session_id}", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["status"] == "expired"


def test_get_consumed_session_reports_consumed() -> None:
    h = Harness()
    row = h.store.add(
        make_row(status="passed", confidence=99.0, completed_at=_now(), consumed_at=_now())
    )

    response = h.client.get(f"/v1/liveness/{row.session_id}", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["status"] == "consumed"


def test_get_requires_service_token() -> None:
    h = Harness()
    row = h.store.add(make_row())
    response = h.client.get(f"/v1/liveness/{row.session_id}")
    assert response.status_code == 401


# ── Step 4: enrolment on pass ────────────────────────────────────────────────


def test_passed_session_enrols_and_consumes() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row.provider_session_id)

    response = h.result(row.session_id)

    assert response.status_code == 200
    assert response.json() == {
        "status": "passed",
        "confidence": 99.5,
        "enrolled": True,
        "reason": None,
    }
    (call,) = h.face_index.index_calls
    assert call["external_image_id"] == str(row.user_ref)  # ExternalImageId = user_ref
    assert call["collection_id"] == "identity-v1"
    assert call["image_bytes"] == b"reference-jpeg-bytes"  # in-memory bytes, not a re-fetch
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
    bindings = {binding for _, binding in h.face_index.faces.values()}
    assert bindings == {str(user_a), str(user_b)}
    assert not hasattr(h.face_index, "search_faces")  # no search path exists to collapse them


def test_quality_rejection_consumes_without_enrolment() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row.provider_session_id)
    h.face_index.next_result = IndexRejected(reasons=("LOW_SHARPNESS",))

    response = h.result(row.session_id)

    assert response.status_code == 200
    assert response.json() == {
        "status": "passed",
        "confidence": 99.5,
        "enrolled": False,
        "reason": "quality_rejected",
    }
    stored = h.store.rows[row.session_id]
    assert stored.status == "consumed" and stored.consumed_at is not None
    assert row.session_id not in h.store.enrolments
    # Consumed, so the session no longer blocks: a FRESH create is allowed
    # (no passed-unconsumed 409 lockout).
    assert h.create(row.user_ref).status_code == 201


def test_transient_index_failure_returns_503_and_consumes_nothing() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row.provider_session_id)
    h.face_index.next_result = FaceIndexUnavailable("IndexFaces failed with ThrottlingException")

    response = h.result(row.session_id)

    assert response.status_code == 503
    envelope = error_body(response)
    assert envelope["code"] == "face_index_unavailable" and envelope["retryable"] is True
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
    h.passed_provider_result(row.provider_session_id)
    first = h.result(row.session_id, key="idem-1")
    assert first.status_code == 200

    replay = h.result(row.session_id, key="idem-1")

    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.json()["enrolled"] is True
    assert len(h.face_index.index_calls) == 1  # replay did NOT re-index
    assert len(h.store.enrolments) == 1


def test_different_key_after_enrolment_is_410() -> None:
    h = Harness()
    row = h.store.add(make_row())
    h.passed_provider_result(row.provider_session_id)
    assert h.result(row.session_id, key="idem-1").status_code == 200

    replay = h.result(row.session_id, key="idem-2")

    assert replay.status_code == 410
    assert error_body(replay)["code"] == "liveness_consumed"
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
    h.passed_provider_result(row.provider_session_id)
    h.result(row.session_id)

    response = h.client.get(f"/v1/liveness/{row.session_id}", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"status": "consumed", "confidence": 99.5, "enrolled": True}
