"""The reviewer hit feed, the verdict route and the operator preview, over
in-memory fakes (repo convention: ``TestClient`` never runs the lifespan; the
store SQL is proven against real Postgres in ``tests/test_review.py`` and
``tests/test_preview_store.py``).

Four properties carry the weight here, and each is easy to undo by accident:

- a malformed cursor is a **422**, never a 500 and never a silent restart at
  page one — a reviewer paging a feed would otherwise re-read the first fifty
  hits believing they had seen the tail;
- the operator preview writes its audit row **before** the crop call
  (INVARIANTS #31) and the row names the operator;
- the ceiling refusal writes **no** audit row, because a refusal is not a
  render (#32);
- the render is the SUBJECT'S render — ``blur=not reveal``, the same argument
  the subject route passes, so #23 stays true structurally rather than by
  promise.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from imageshield.preview.store import OperatorPreviewTarget
from imageshield.review.store import HitsPage, VerdictRecord
from imageshield.types import UserRef
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config

AUTH = {"X-Service-Token": SERVICE_TOKEN}
ADMIN = {**AUTH, "X-Admin-Service-Token": ADMIN_SERVICE_TOKEN}
BBOX = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
IMAGE_URL = "https://cdn.example/hit.webp"


def _hit(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "infringement_id": uuid4(),
        "user_ref": UserRef(uuid4()),
        "source_domain": "example.test",
        "page_url": "https://example.test/p",
        "image_url": "https://example.test/p.jpg",
        "first_seen_at": datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
        "status": "new",
        "confirm_state": "machine_triaged",
        "severity": "explicit_unmatched",
        "confirm_decided_by": None,
        "confirm_decided_at": None,
        "face_match_score": 91.25,
        "moderation_labels": ["Explicit Nudity"],
        "duplicate_of": None,
        "preview_available": True,
        "review_task": None,
        "subject_decision": None,
        "latest_verdict": None,
        "source_object_ref": "photo/abc",
        "seed_kind": "face_crop",
    }
    base.update(overrides)
    return base


class FakeReviewStore:
    """Only the two methods these routes call. Every other ``ReviewStore``
    attribute raises, so a route quietly reaching for one is a loud failure
    rather than a silent ``None``."""

    def __init__(
        self,
        *,
        page: HitsPage | None = None,
        record: VerdictRecord | None = None,
    ) -> None:
        self._page = page if page is not None else HitsPage(hits=(), has_more=False)
        self._record = record
        self.list_calls: list[dict[str, Any]] = []
        self.verdict_calls: list[dict[str, Any]] = []

    async def list_hits(self, **kwargs: Any) -> HitsPage:
        self.list_calls.append(kwargs)
        return self._page

    async def record_verdict(
        self, infringement_id: UUID, *, operator: str, verdict: str, note: str | None
    ) -> VerdictRecord | None:
        self.verdict_calls.append(
            {
                "infringement_id": infringement_id,
                "operator": operator,
                "verdict": verdict,
                "note": note,
            }
        )
        return self._record

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"this route must not call {name!r}")


class RecordingPreviewStore:
    """Records the ORDER of its calls, interleaved with the crop client's, so
    "the audit lands before the render" is asserted rather than assumed."""

    def __init__(
        self,
        target: OperatorPreviewTarget | None,
        *,
        renders: int = 0,
        trace: list[str] | None = None,
    ) -> None:
        self._target = target
        self._renders = renders
        self.trace = trace if trace is not None else []
        self.recorded: list[tuple[str, UserRef, UUID, bool]] = []
        self.counted: list[str] = []

    async def operator_target(self, infringement_id: UUID) -> OperatorPreviewTarget | None:
        return self._target

    async def operator_renders_last_24h(self, operator: str) -> int:
        self.counted.append(operator)
        return self._renders

    async def record_operator_render(
        self, operator: str, user_ref: UserRef, infringement_id: UUID, *, reveal: bool
    ) -> None:
        self.trace.append("audit")
        self.recorded.append((operator, user_ref, infringement_id, reveal))


class RecordingCropClient:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.calls: list[dict[str, object]] = []

    async def crop(self, *, url: str, bbox: dict[str, float], blur: bool) -> bytes:
        self.trace.append("crop")
        self.calls.append({"url": url, "bbox": bbox, "blur": blur})
        return b"jpeg-bytes"


def make_client(
    *,
    page: HitsPage | None = None,
    record: VerdictRecord | None = None,
    target: OperatorPreviewTarget | None = None,
    renders: int = 0,
) -> tuple[TestClient, FakeReviewStore, RecordingPreviewStore, RecordingCropClient]:
    app = create_app(config=make_config())
    review = FakeReviewStore(page=page, record=record)
    trace: list[str] = []
    preview = RecordingPreviewStore(target, renders=renders, trace=trace)
    crop = RecordingCropClient(trace)
    app.state.review_store = review
    app.state.preview_store = preview
    app.state.crop_client = crop
    return TestClient(app), review, preview, crop


# ── auth ─────────────────────────────────────────────────────────────────


def test_every_route_needs_both_tokens() -> None:
    client, review, preview, _crop = make_client()
    identifier = uuid4()

    assert client.get("/v1/admin/hits").status_code == 401
    assert client.get("/v1/admin/hits", headers=AUTH).status_code == 401
    verdict_path = f"/v1/admin/hits/{identifier}/verdict"
    body = {"verdict": "true_positive", "operator": "alice"}
    assert client.post(verdict_path, json=body).status_code == 401
    assert client.post(verdict_path, json=body, headers=AUTH).status_code == 401
    preview_path = f"/v1/admin/infringements/{identifier}/preview"
    assert client.get(preview_path, params={"operator": "alice"}).status_code == 401
    assert client.get(preview_path, params={"operator": "alice"}, headers=AUTH).status_code == 401

    assert review.list_calls == []
    assert review.verdict_calls == []
    assert preview.recorded == []


# ── the feed and its cursor ──────────────────────────────────────────────


def test_a_cursor_round_trips_to_the_next_page() -> None:
    """The cursor the first page issues is the position the second page asks
    for — decoded back into the exact (first_seen_at, infringement_id) of the
    last row served, not a re-parsed approximation of it."""
    last = _hit(first_seen_at=datetime(2026, 9, 14, 11, 30, tzinfo=UTC))
    client, review, _preview, _crop = make_client(page=HitsPage(hits=(_hit(), last), has_more=True))

    first = client.get("/v1/admin/hits", params={"limit": 2}, headers=ADMIN)

    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor is not None

    client.get("/v1/admin/hits", params={"cursor": cursor}, headers=ADMIN)

    assert review.list_calls[-1]["after"] == (
        last["first_seen_at"],
        last["infringement_id"],
    )


def test_the_last_page_has_no_next_cursor() -> None:
    """`null`, not an empty string: a client must not have to special-case a
    falsy cursor to know it is done."""
    client, _review, _preview, _crop = make_client(page=HitsPage(hits=(_hit(),), has_more=False))

    response = client.get("/v1/admin/hits", headers=ADMIN)

    assert response.status_code == 200
    assert response.json()["next_cursor"] is None


def test_a_corrupt_cursor_is_a_422_and_never_reaches_the_store() -> None:
    """Silently ignoring it is the dangerous failure: the reviewer restarts at
    page one and re-reads the same fifty hits believing they reached the
    tail."""
    client, review, _preview, _crop = make_client()

    response = client.get("/v1/admin/hits", params={"cursor": "not-a-cursor"}, headers=ADMIN)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cursor"
    assert review.list_calls == []


def test_a_well_formed_but_nonsense_cursor_is_also_a_422() -> None:
    """Valid base64 carrying rubbish — the decode has to fail on the CONTENT,
    not only on the encoding."""
    client, review, _preview, _crop = make_client()
    cursor = base64.urlsafe_b64encode(b"yesterday|not-a-uuid").decode()

    response = client.get("/v1/admin/hits", params={"cursor": cursor}, headers=ADMIN)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cursor"
    assert review.list_calls == []


def test_the_filters_reach_the_store_and_quarantined_is_not_offered() -> None:
    client, review, _preview, _crop = make_client()
    user_ref = uuid4()

    response = client.get(
        "/v1/admin/hits",
        params={
            "severity": "benign_copy",
            "confirm_state": "rejected",
            "user_ref": str(user_ref),
            "since": "2026-09-01T00:00:00Z",
        },
        headers=ADMIN,
    )
    refused = client.get("/v1/admin/hits", params={"confirm_state": "quarantined"}, headers=ADMIN)

    assert response.status_code == 200
    call = review.list_calls[0]
    assert call["severity"] == "benign_copy"
    assert call["confirm_state"] == "rejected"
    assert call["user_ref"] == user_ref
    assert call["since"] == datetime(2026, 9, 1, tzinfo=UTC)
    # `quarantined` is not in the filter Literal at all. The store excludes it
    # unconditionally, so a filter for it could only ever return nothing —
    # offering it would be offering a query that lies about its emptiness.
    assert refused.status_code == 422
    assert len(review.list_calls) == 1


def test_a_hit_serialises_with_its_nested_objects() -> None:
    client, _review, _preview, _crop = make_client(
        page=HitsPage(
            hits=(
                _hit(
                    confirm_state="rejected",
                    confirm_decided_by="subject",
                    subject_decision={
                        "decision": "rejected",
                        "decided_at": datetime(2026, 9, 14, 13, 0, tzinfo=UTC),
                    },
                    latest_verdict={
                        "verdict_id": uuid4(),
                        "operator": "alice",
                        "verdict": "false_positive",
                        "note": "wrong face",
                        "created_at": datetime(2026, 9, 14, 14, 0, tzinfo=UTC),
                    },
                ),
            ),
            has_more=False,
        )
    )

    body = client.get("/v1/admin/hits", headers=ADMIN).json()

    (hit,) = body["hits"]
    assert hit["subject_decision"]["decision"] == "rejected"
    assert hit["latest_verdict"]["verdict"] == "false_positive"
    assert hit["moderation_labels"] == ["Explicit Nudity"]
    assert hit["preview_available"] is True
    assert hit["seed_kind"] == "face_crop"


# ── the verdict ──────────────────────────────────────────────────────────


def _record(**overrides: Any) -> VerdictRecord:
    base: dict[str, Any] = {
        "verdict_id": uuid4(),
        "infringement_id": uuid4(),
        "operator": "alice",
        "verdict": "false_positive",
        "note": "wrong face",
        "machine_severity": "explicit_unmatched",
        "face_match_score": 91.25,
        "confirm_state_at_verdict": "machine_triaged",
        "created_at": datetime(2026, 9, 14, 14, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return VerdictRecord(**base)


def test_a_verdict_is_201_with_the_created_row() -> None:
    record = _record()
    client, review, _preview, _crop = make_client(record=record)
    infringement_id = uuid4()

    response = client.post(
        f"/v1/admin/hits/{infringement_id}/verdict",
        json={"verdict": "false_positive", "operator": "alice", "note": "wrong face"},
        headers=ADMIN,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["verdict"] == "false_positive"
    assert body["operator"] == "alice"
    assert body["machine_severity"] == "explicit_unmatched"
    assert body["confirm_state_at_verdict"] == "machine_triaged"
    assert review.verdict_calls == [
        {
            "infringement_id": infringement_id,
            "operator": "alice",
            "verdict": "false_positive",
            "note": "wrong face",
        }
    ]


def test_an_absent_or_quarantined_hit_is_a_404() -> None:
    client, _review, _preview, _crop = make_client(record=None)

    response = client.post(
        f"/v1/admin/hits/{uuid4()}/verdict",
        json={"verdict": "unsure", "operator": "alice"},
        headers=ADMIN,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "infringement_not_found"


def test_an_unsigned_or_unknown_verdict_is_a_422() -> None:
    """`operator` is required because a measurement nobody signed is not a
    measurement; `extra='forbid'` means a typo'd field is refused rather than
    dropped."""
    client, review, _preview, _crop = make_client(record=_record())
    path = f"/v1/admin/hits/{uuid4()}/verdict"

    unsigned = client.post(path, json={"verdict": "unsure"}, headers=ADMIN)
    empty = client.post(path, json={"verdict": "unsure", "operator": ""}, headers=ADMIN)
    unknown = client.post(path, json={"verdict": "probably", "operator": "alice"}, headers=ADMIN)
    extra = client.post(
        path,
        json={"verdict": "unsure", "operator": "alice", "severity": "benign_copy"},
        headers=ADMIN,
    )

    assert [r.status_code for r in (unsigned, empty, unknown, extra)] == [422] * 4
    assert review.verdict_calls == []


# ── the operator preview ─────────────────────────────────────────────────


def _target(**overrides: Any) -> OperatorPreviewTarget:
    base: dict[str, Any] = {
        "image_url": IMAGE_URL,
        "bbox": BBOX,
        "user_ref": UserRef(uuid4()),
    }
    base.update(overrides)
    return OperatorPreviewTarget(**base)


def test_the_audit_row_is_written_before_the_crop_and_names_the_operator() -> None:
    """INVARIANTS #31, and the reason the operator preview was allowed at all:
    a staff view of somebody's hit imagery that is not attributable to a named
    person is not an audited view."""
    target = _target()
    client, _review, preview, _crop = make_client(target=target)
    infringement_id = uuid4()

    response = client.get(
        f"/v1/admin/infringements/{infringement_id}/preview",
        params={"operator": "alice"},
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert preview.trace == ["audit", "crop"]
    assert preview.recorded == [("alice", target.user_ref, infringement_id, False)]
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == b"jpeg-bytes"


def test_reveal_sharpens_the_face_exactly_as_the_subject_path_does() -> None:
    """`blur=not reveal` — the same argument the subject route passes, which
    is what makes INVARIANTS #23 structurally true here rather than promised.
    On the fetcher's side `blur=False` means "sharpen the face box", never
    "return the frame sharp"; there is no operator render mode."""
    client, _review, preview, crop = make_client(target=_target())

    blurred = client.get(
        f"/v1/admin/infringements/{uuid4()}/preview",
        params={"operator": "alice"},
        headers=ADMIN,
    )
    revealed = client.get(
        f"/v1/admin/infringements/{uuid4()}/preview",
        params={"operator": "alice", "reveal": "true"},
        headers=ADMIN,
    )

    assert (blurred.status_code, revealed.status_code) == (200, 200)
    assert [call["blur"] for call in crop.calls] == [True, False]
    assert [call["url"] for call in crop.calls] == [IMAGE_URL, IMAGE_URL]
    assert [call["bbox"] for call in crop.calls] == [BBOX, BBOX]
    assert [row[3] for row in preview.recorded] == [False, True]


def test_a_missing_operator_is_a_422_and_renders_nothing() -> None:
    """An unattributable render is the one thing this route may not do."""
    client, _review, preview, crop = make_client(target=_target())

    response = client.get(f"/v1/admin/infringements/{uuid4()}/preview", headers=ADMIN)

    assert response.status_code == 422
    assert preview.recorded == []
    assert crop.calls == []


def test_an_absent_or_quarantined_hit_is_a_404_with_no_audit_row() -> None:
    client, _review, preview, crop = make_client(target=None)

    response = client.get(
        f"/v1/admin/infringements/{uuid4()}/preview",
        params={"operator": "alice"},
        headers=ADMIN,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "infringement_not_found"
    assert preview.recorded == []
    assert crop.calls == []


def test_a_hit_with_no_image_or_no_bbox_is_preview_unavailable() -> None:
    for target in (_target(image_url=None), _target(bbox=None)):
        client, _review, preview, crop = make_client(target=target)

        response = client.get(
            f"/v1/admin/infringements/{uuid4()}/preview",
            params={"operator": "alice"},
            headers=ADMIN,
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "preview_unavailable"
        assert preview.recorded == []
        assert crop.calls == []


def test_the_ceiling_is_a_429_and_writes_no_audit_row() -> None:
    """A refusal is not an attempt: nothing was rendered, so nothing is
    audited and nothing is charged. The default ceiling is deliberately far
    above honest review throughput — this test reaches it by construction, not
    by working."""
    ceiling = make_config().review_operator_daily_render_ceiling
    client, _review, preview, crop = make_client(target=_target(), renders=ceiling)

    response = client.get(
        f"/v1/admin/infringements/{uuid4()}/preview",
        params={"operator": "alice"},
        headers=ADMIN,
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "preview_rate_limited"
    assert preview.counted == ["alice"]
    assert preview.recorded == []
    assert crop.calls == []
