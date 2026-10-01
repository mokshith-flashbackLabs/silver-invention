"""``/v1/admin/intel/source-proposals``, ``/source-validations`` and ``/weight-suggestions``
(spec §4.6, §4.10): shape, refusals and rendering, over fakes. The runs are tested against
Postgres in test_intel_question_runs.py, the store in test_intel_question_store.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from imageshield.intel.models import Run, Source
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config
from tests.question_fakes import QUESTION

ADMIN = {"X-Service-Token": SERVICE_TOKEN, "X-Admin-Service-Token": ADMIN_SERVICE_TOKEN}


def _now() -> datetime:
    return datetime.now(UTC)


def _run(
    kind: str,
    *,
    status: str = "completed",
    outcome: dict[str, Any] | None = None,
    completed_at: datetime | None = None,
    request: dict[str, Any] | None = None,
) -> Run:
    return Run(
        run_id=uuid4(),
        kind=kind,
        source_id=None,
        request=request or {},
        status=status,
        attempts=1,
        requested_by="ann",
        outcome=outcome or {},
        error_code=None,
        created_at=_now(),
        completed_at=completed_at or (_now() if status == "completed" else None),
    )


def _source_row(url: str = "https://p.example/terms") -> Source:
    now = _now()
    return Source(
        source_id=uuid4(),
        kind="policy_page",
        source_url=url,
        url_hash="a" * 64,
        query_text=None,
        tags=("instagram",),
        check_every_hours=168,
        next_check_at=now,
        enabled=True,
        terms_note="automated access permitted",
        last_content_sha256=None,
        last_checked_at=None,
        last_run_status=None,
        consecutive_failures=0,
        disabled_reason=None,
        created_by="ann",
        created_at=now,
    )


class FakeQuestionStore:
    def __init__(self) -> None:
        self.queued: list[tuple[str, dict[str, Any], str]] = []
        self.runs: dict[UUID, Run] = {}
        self.sources: dict[UUID, Source] = {}

    async def queue_source_proposal(self, request: dict[str, Any], *, operator: str) -> UUID:
        self.queued.append(("source_proposal", request, operator))
        return uuid4()

    async def queue_source_validation(self, request: dict[str, Any], *, operator: str) -> UUID:
        self.queued.append(("source_validation", request, operator))
        return uuid4()

    async def get_run(self, run_id: UUID) -> Run | None:
        return self.runs.get(run_id)

    async def sources_by_ids(self, source_ids: Any) -> list[Source]:
        return [self.sources[i] for i in source_ids if i in self.sources]


def _client() -> tuple[TestClient, FakeQuestionStore]:
    app = create_app(config=make_config())
    questions = FakeQuestionStore()
    app.state.question_store = questions
    return TestClient(app), questions


def _post(client: TestClient, path: str, body: dict[str, Any]) -> Any:
    return client.post(f"/v1/admin/intel/{path}", json=body, headers=ADMIN)


# ── stage 1 ───────────────────────────────────────────────────────────────────


def test_a_source_proposal_is_queued_with_the_question_and_never_the_operator() -> None:
    client, questions = _client()
    r = _post(client, "source-proposals", {**QUESTION, "operator": "ann"})
    assert r.status_code == 202 and UUID(r.json()["run_id"])
    ((kind, request, operator),) = questions.queued
    assert (kind, operator) == ("source_proposal", "ann")
    assert request == {
        "question_key": "platforms",
        "prompt": QUESTION["prompt"],
        "options": ["Instagram", "Bumble"],
        "tags": {"Instagram": ["instagram"]},
    }


def test_a_question_sent_without_tags_stores_no_tags_key() -> None:
    """spec §4.6: an absent map means "use the vocabulary's"; ``{}`` means "no tags"."""
    client, questions = _client()
    body = {k: v for k, v in QUESTION.items() if k != "tags"}
    assert _post(client, "source-proposals", {**body, "operator": "ann"}).status_code == 202
    assert "tags" not in questions.queued[0][1]


@pytest.mark.parametrize(
    "change",
    [
        {"options": ["Instagram", "Instagram"]},
        {"options": []},
        {"options": ["   "]},
        {"tags": {"Tinder": ["tinder"]}},  # a key that is not an option
        {"tags": {"Instagram": ["Insta-Gram"]}},
        {"tags": {"Instagram": ["instagram", "instagram"]}},
        {"operator": ""},
        {"surprise": 1},
    ],
)
def test_a_malformed_question_is_422(change: dict[str, Any]) -> None:
    client, questions = _client()
    r = _post(client, "source-proposals", {**QUESTION, "operator": "ann", **change})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert questions.queued == []


def test_the_source_proposal_poll_is_404_for_an_unknown_run_or_another_kind() -> None:
    client, questions = _client()
    r = client.get(f"/v1/admin/intel/source-proposals/{uuid4()}", headers=ADMIN)
    assert r.status_code == 404 and r.json()["error"]["code"] == "intel_run_not_found"
    other = _run("source_validation")
    questions.runs[other.run_id] = other
    r = client.get(f"/v1/admin/intel/source-proposals/{other.run_id}", headers=ADMIN)
    assert r.status_code == 404 and r.json()["error"]["code"] == "intel_run_not_found"


def test_the_source_proposal_poll_renders_existing_sources_and_candidates() -> None:
    client, questions = _client()
    source = _source_row()
    questions.sources[source.source_id] = source
    candidate = {
        "kind": "search_query",
        "source_url": None,
        "query_text": "Instagram privacy change",
        "reason": "news",
    }
    options = [
        {
            "option": "Instagram",
            "tags": ["instagram"],
            "existing": [str(source.source_id)],
            "proposed": [candidate],
        }
    ]
    run = _run("source_proposal", outcome={"model_calls": 1, "options": options})
    questions.runs[run.run_id] = run
    body = client.get(f"/v1/admin/intel/source-proposals/{run.run_id}", headers=ADMIN).json()
    assert (body["status"], body["error_code"]) == ("completed", None)
    (option,) = body["options"]
    assert option["existing"][0]["source_id"] == str(source.source_id)
    assert option["existing"][0]["origin"] == "operator"
    assert option["proposed"] == [candidate]


def test_a_queued_source_proposal_polls_with_no_options() -> None:
    client, questions = _client()
    run = _run("source_proposal", status="queued")
    questions.runs[run.run_id] = run
    body = client.get(f"/v1/admin/intel/source-proposals/{run.run_id}", headers=ADMIN).json()
    assert (body["status"], body["options"]) == ("queued", None)


# ── stage 3 ───────────────────────────────────────────────────────────────────


def _candidate(**changes: Any) -> dict[str, Any]:
    return {
        "option": "Instagram",
        "kind": "policy_page",
        "source_url": "https://p.example/terms",
        **changes,
    }


def test_a_validation_is_queued_with_the_candidates_as_sent() -> None:
    client, questions = _client()
    query = {"option": "Bumble", "kind": "search_query", "query_text": "Bumble privacy news"}
    r = _post(
        client, "source-validations", {"candidates": [_candidate(), query], "operator": "ann"}
    )
    assert r.status_code == 202 and UUID(r.json()["run_id"])
    ((kind, request, operator),) = questions.queued
    assert (kind, operator) == ("source_validation", "ann")
    assert request == {
        "candidates": [
            {
                "option": "Instagram",
                "kind": "policy_page",
                "source_url": "https://p.example/terms",
                "query_text": None,
            },
            {
                "option": "Bumble",
                "kind": "search_query",
                "source_url": None,
                "query_text": "Bumble privacy news",
            },
        ]
    }


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(kind="search_query"),  # a query kind carrying a URL
        _candidate(source_url="http://p.example/terms"),
        _candidate(source_url=None),
        _candidate(query_text="also a query"),
        _candidate(option=""),
        _candidate(kind="bogus"),
    ],
)
def test_a_malformed_candidate_is_422(candidate: dict[str, Any]) -> None:
    client, questions = _client()
    r = _post(client, "source-validations", {"candidates": [candidate], "operator": "ann"})
    assert r.status_code == 422 and questions.queued == []


def test_a_validation_takes_up_to_250_candidates() -> None:
    client, _ = _client()
    many = [_candidate(source_url=f"https://p.example/{i}") for i in range(251)]
    assert (
        _post(client, "source-validations", {"candidates": many, "operator": "a"}).status_code
        == 422
    )
    body = {"candidates": many[:250], "operator": "a"}
    assert _post(client, "source-validations", body).status_code == 202


def test_the_validation_poll_answers_an_object_with_ordered_results_and_an_expiry() -> None:
    client, questions = _client()
    results = [
        {"candidate": _candidate(query_text=None), "status": "ready", "reason": None},
        {
            "candidate": _candidate(source_url="https://p.example/app", query_text=None),
            "status": "blocked",
            "reason": "too_short",
        },
    ]
    done = _now()
    run = _run("source_validation", outcome={"results": results}, completed_at=done)
    questions.runs[run.run_id] = run
    body = client.get(f"/v1/admin/intel/source-validations/{run.run_id}", headers=ADMIN).json()
    assert (body["status"], body["results"]) == ("completed", results)
    assert datetime.fromisoformat(body["honoured_until"]) == done + timedelta(hours=24)
    queued = _run("source_validation", status="queued")
    questions.runs[queued.run_id] = queued
    body = client.get(f"/v1/admin/intel/source-validations/{queued.run_id}", headers=ADMIN).json()
    assert (body["results"], body["honoured_until"]) == (None, None)
    other = _run("source_proposal")
    questions.runs[other.run_id] = other
    r = client.get(f"/v1/admin/intel/source-validations/{other.run_id}", headers=ADMIN)
    assert r.status_code == 404
