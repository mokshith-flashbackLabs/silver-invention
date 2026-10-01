"""``/v1/admin/intel/source-proposals``, ``/source-validations`` and ``/weight-suggestions``
(spec §4.6, §4.10): shape, refusals and rendering, over fakes. The runs are tested against
Postgres in test_intel_question_runs.py, the store in test_intel_question_store.py."""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from imageshield.intel.models import Run, Source, Vocabulary
from imageshield.intel.source_choice import (
    NewSource,
    Registered,
    Validation,
    candidate_key,
    identity,
)
from imageshield.search.urlhash import url_hash
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config
from tests.intel_fakes import QUIZ_VOCABULARY
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
        self.suggestions: dict[UUID, tuple[UUID, list[dict[str, Any]]]] = {}
        self.sources: dict[UUID, Source] = {}
        self.validation_records: dict[UUID, Validation] = {}
        self.proposed: frozenset[str] = frozenset()
        self.registrations: list[tuple[dict[str, Any], list[NewSource], str]] = []

    async def queue_source_proposal(self, request: dict[str, Any], *, operator: str) -> UUID:
        self.queued.append(("source_proposal", request, operator))
        return uuid4()

    async def queue_source_validation(self, request: dict[str, Any], *, operator: str) -> UUID:
        self.queued.append(("source_validation", request, operator))
        return uuid4()

    async def get_run(self, run_id: UUID) -> Run | None:
        return self.runs.get(run_id)

    async def suggestion_of_run(self, run_id: UUID) -> tuple[UUID, list[dict[str, Any]]] | None:
        return self.suggestions.get(run_id)

    async def options_of_suggestion(self, proposal_id: UUID) -> list[dict[str, Any]]:
        return next((o for pid, o in self.suggestions.values() if pid == proposal_id), [])

    async def sources_by_ids(self, source_ids: Any) -> list[Source]:
        return [self.sources[i] for i in source_ids if i in self.sources]

    async def validations(self, run_ids: Any) -> dict[UUID, Validation]:
        return {i: self.validation_records[i] for i in run_ids if i in self.validation_records}

    async def proposed_identities(self, question_key: str, *, since: datetime) -> frozenset[str]:
        return self.proposed

    async def register_and_queue_suggestion(
        self, request: dict[str, Any], sources: Any, *, operator: str
    ) -> Registered:
        self.registrations.append((request, list(sources), operator))
        return Registered(uuid4(), (), ())


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
        _candidate(option="   "),  # blank after trimming
        _candidate(source_url="https://"),  # no host is malformed, not "check again later"
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


# ── stage 4 ───────────────────────────────────────────────────────────────────

VALIDATION_RUN = uuid4()
TERMS = "https://p.example/terms"
TERMS_KEY = candidate_key("policy_page", TERMS, None)
NOTE = "public terms page; automated reads allowed"


class FakeRegistryStore:
    """What the stage-4 route reads from the intel store: known hits and the vocabulary
    (QUIZ_VOCABULARY: instagram mapped and active, linkedin active, myspace retired)."""

    def __init__(self) -> None:
        self.known_hits: set[str] = set()

    async def is_known_hit(self, value: str) -> bool:
        return value in self.known_hits

    async def load_vocabulary(self) -> Vocabulary:
        return Vocabulary(
            release_no=2,
            map_version=1,
            scoring_version="s2",
            quiz_version="q",
            document=copy.deepcopy(QUIZ_VOCABULARY),
        )


def _suggest_client() -> tuple[TestClient, FakeQuestionStore, FakeRegistryStore]:
    app = create_app(config=make_config())
    questions, registry = FakeQuestionStore(), FakeRegistryStore()
    app.state.question_store = questions
    app.state.intel_store = registry
    return TestClient(app), questions, registry


def _chosen(**changes: Any) -> dict[str, Any]:
    return {
        "option": "Instagram",
        "kind": "policy_page",
        "source_url": TERMS,
        "validation_run_id": str(VALIDATION_RUN),
        "terms_note": NOTE,
        **changes,
    }


def _suggest(sources: list[dict[str, Any]], **changes: Any) -> dict[str, Any]:
    return {
        **QUESTION,
        "type": "mutable",
        "cap": 8,
        "sources": sources,
        "operator": "ann",
        **changes,
    }


def _ready(
    questions: FakeQuestionStore, *keys: tuple[str, str], completed_at: datetime | None = None
) -> None:
    questions.validation_records[VALIDATION_RUN] = Validation(
        completed_at or _now(), frozenset(keys)
    )


def test_a_suggestion_registers_its_validated_sources_and_queues_the_run() -> None:
    client, questions, _ = _suggest_client()
    _ready(questions, TERMS_KEY)
    body = _suggest([_chosen(source_url=f"{TERMS}?utm_source=x")])  # the same canonical URL
    r = _post(client, "weight-suggestions", body)
    assert r.status_code == 202 and UUID(r.json()["run_id"])
    ((request, sources, operator),) = questions.registrations
    assert operator == "ann"
    assert request == {
        "question_key": "platforms",
        "prompt": QUESTION["prompt"],
        "options": ["Instagram", "Bumble"],
        "tags": {"Instagram": ["instagram"]},
        "type": "mutable",
        "cap": 8,
    }
    assert sources == [
        NewSource(
            kind="policy_page",
            source_url=TERMS,
            query_text=None,
            tags=("instagram",),
            check_every_hours=168,
            terms_note=NOTE,
            origin="operator",
            question_key="platforms",
            option="Instagram",
        )
    ]


@pytest.mark.parametrize(
    ("setup", "reason"),
    [("missing", "unknown_run"), ("stale", "expired"), ("blocked", "not_ready")],
)
def test_a_source_that_is_not_validated_is_refused_and_nothing_registered(
    setup: str, reason: str
) -> None:
    """spec §10: POST /weight-suggestions refuses a source that is not ready in the named
    validation run, or whose result is older than 24 hours, and registers nothing."""
    client, questions, _ = _suggest_client()
    if setup == "stale":
        _ready(questions, TERMS_KEY, completed_at=_now() - timedelta(hours=25))
    elif setup == "blocked":
        _ready(questions)  # the run completed and found nothing ready
    r = _post(client, "weight-suggestions", _suggest([_chosen()]))
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "source_not_validated"
    assert error["entries"] == [{"index": 0, "reason": reason}]
    assert {"code", "message", "retryable", "request_id", "entries"} <= set(error)
    assert questions.registrations == []


def test_only_the_failing_entries_are_named_by_index_and_kind_matters() -> None:
    client, questions, _ = _suggest_client()
    _ready(questions, TERMS_KEY)
    sources = [_chosen(), _chosen(source_url="https://p.example/other"), _chosen(kind="news")]
    r = _post(client, "weight-suggestions", _suggest(sources))
    entries = r.json()["error"]["entries"]
    assert entries == [
        {"index": 1, "reason": "not_ready"},
        {"index": 2, "reason": "not_ready"},  # ready as a policy_page is not ready as news
    ]
    pattern = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
    assert all(pattern.fullmatch(e["reason"]) for e in entries)
    assert questions.registrations == []


def test_a_known_hit_or_a_person_shaped_query_is_refused_even_when_validated() -> None:
    client, questions, registry = _suggest_client()
    leak = "leaks about jane@example.com"
    _ready(questions, TERMS_KEY, candidate_key("search_query", None, leak))
    chosen = _chosen(kind="search_query", source_url=None, query_text=leak)
    r = _post(client, "weight-suggestions", _suggest([chosen]))
    assert r.json()["error"]["code"] == "query_names_a_person"
    registry.known_hits.add(url_hash(TERMS))
    r = _post(client, "weight-suggestions", _suggest([_chosen()]))
    assert r.json()["error"]["code"] == "known_hit_location"
    assert questions.registrations == []


def test_an_unknown_tag_is_refused_and_a_retired_one_is_dropped() -> None:
    client, questions, _ = _suggest_client()
    _ready(questions, TERMS_KEY)
    body = _suggest([_chosen()], tags={"Instagram": ["instagram", "bumble"]})
    r = _post(client, "weight-suggestions", body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_tag"
    assert r.json()["error"]["slugs"] == ["bumble"] and questions.registrations == []
    body = _suggest([_chosen()], tags={"Instagram": ["instagram", "myspace"]})
    assert _post(client, "weight-suggestions", body).status_code == 202
    assert questions.registrations[-1][1][0].tags == ("instagram",)


def test_an_unknown_tag_on_an_option_with_no_chosen_source_is_not_refused() -> None:
    client, questions, _ = _suggest_client()
    _ready(questions, TERMS_KEY)
    body = _suggest([_chosen()], tags={"Instagram": ["instagram"], "Bumble": ["bumble"]})
    assert _post(client, "weight-suggestions", body).status_code == 202


def test_the_vocabularys_map_is_used_only_when_the_request_sends_none() -> None:
    client, questions, _ = _suggest_client()
    _ready(questions, TERMS_KEY)
    body = _suggest([_chosen()])
    del body["tags"]
    assert _post(client, "weight-suggestions", body).status_code == 202
    assert questions.registrations[-1][1][0].tags == ("instagram",)  # QUIZ_VOCABULARY's map
    assert _post(client, "weight-suggestions", _suggest([_chosen()], tags={})).status_code == 202
    assert questions.registrations[-1][1][0].tags == ()


def test_a_source_the_stage_one_run_proposed_registers_as_suggested() -> None:
    client, questions, _ = _suggest_client()
    _ready(questions, TERMS_KEY)
    questions.proposed = frozenset({identity("policy_page", TERMS, None)})
    body = _suggest([_chosen(check_every_hours=24)])
    assert _post(client, "weight-suggestions", body).status_code == 202
    source = questions.registrations[-1][1][0]
    assert (source.origin, source.check_every_hours) == ("suggested", 24)


def test_one_source_chosen_for_two_options_is_registered_once_with_both_tags() -> None:
    client, questions, _ = _suggest_client()
    _ready(questions, TERMS_KEY)
    tags = {"Instagram": ["instagram"], "Bumble": ["linkedin"]}
    body = _suggest([_chosen(), _chosen(option="Bumble")], tags=tags)
    assert _post(client, "weight-suggestions", body).status_code == 202
    (source,) = questions.registrations[-1][1]
    assert (source.option, source.tags) == ("Instagram", ("instagram", "linkedin"))


def test_a_suggestion_without_sources_needs_no_validation() -> None:
    client, questions, _ = _suggest_client()
    assert _post(client, "weight-suggestions", _suggest([])).status_code == 202
    assert questions.registrations[-1][1] == []


@pytest.mark.parametrize(
    "change",
    [
        {"sources": [_chosen(option="Tinder")]},  # not one of the options
        {"cap": 11},
        {"sources": [_chosen(terms_note="short")]},
        {"sources": [_chosen(check_every_hours=5)]},
        {"sources": [_chosen(validation_run_id="not-a-uuid")]},
        {"sources": [_chosen(source_url="http://p.example/terms")]},
    ],
)
def test_a_malformed_suggestion_body_is_422(change: dict[str, Any]) -> None:
    client, questions, _ = _suggest_client()
    r = _post(client, "weight-suggestions", {**_suggest([]), **change})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert questions.registrations == []


def test_type_is_required_but_may_be_null_and_cap_may_be_omitted() -> None:
    client, questions, _ = _suggest_client()
    body = _suggest([], type=None)
    del body["cap"]
    assert _post(client, "weight-suggestions", body).status_code == 202
    request = questions.registrations[-1][0]
    assert (request["type"], request["cap"]) == (None, None)
    missing = _suggest([])
    del missing["type"]
    assert _post(client, "weight-suggestions", missing).status_code == 422


# ── the suggestion's reads ────────────────────────────────────────────────────

OPTIONS = [
    {
        "option": "Instagram",
        "deduction": 4,
        "rationale": "r",
        "signal_ids": [],
        "suggested_tags": ["instagram"],
        "new_tag": None,
        "corroborated": False,
        "why_not": "no_evidence",
    }
]


def test_the_suggestion_poll_answers_options_once_written_and_counts_deferred_sources() -> None:
    client, questions = _client()
    r = client.get(f"/v1/admin/intel/weight-suggestions/{uuid4()}", headers=ADMIN)
    assert r.status_code == 404 and r.json()["error"]["code"] == "intel_run_not_found"
    queued = _run("weight_suggestion", status="queued")
    questions.runs[queued.run_id] = queued
    body = client.get(f"/v1/admin/intel/weight-suggestions/{queued.run_id}", headers=ADMIN).json()
    assert (body["status"], body["proposal_id"], body["options"]) == ("queued", None, None)
    done = _run("weight_suggestion", outcome={"sources_deferred": 3})
    proposal_id = uuid4()
    questions.runs[done.run_id] = done
    questions.suggestions[done.run_id] = (proposal_id, OPTIONS)
    body = client.get(f"/v1/admin/intel/weight-suggestions/{done.run_id}", headers=ADMIN).json()
    assert body["proposal_id"] == str(proposal_id) and body["options"] == OPTIONS
    assert (body["status"], body["error_code"], body["sources_deferred"]) == ("completed", None, 3)
    other = _run("source_validation")
    questions.runs[other.run_id] = other
    r = client.get(f"/v1/admin/intel/weight-suggestions/{other.run_id}", headers=ADMIN)
    assert r.status_code == 404


class FakeProposals:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def get_proposal(self, proposal_id: UUID) -> dict[str, Any] | None:
        return next((r for r in self.rows if r["proposal_id"] == proposal_id), None)


def test_a_suggestions_detail_carries_its_options_and_other_kinds_do_not() -> None:
    suggestion = {"proposal_id": uuid4(), "kind": "weight_suggestion", "status": "delivered"}
    change = {"proposal_id": uuid4(), "kind": "weight_change", "status": "pending"}
    app = create_app(config=make_config())
    questions = FakeQuestionStore()
    questions.suggestions[uuid4()] = (suggestion["proposal_id"], OPTIONS)
    app.state.proposal_store = FakeProposals([suggestion, change])
    app.state.question_store = questions
    client = TestClient(app)
    detail = client.get(f"/v1/admin/intel/proposals/{suggestion['proposal_id']}", headers=ADMIN)
    assert detail.status_code == 200 and detail.json()["options"] == OPTIONS
    other = client.get(f"/v1/admin/intel/proposals/{change['proposal_id']}", headers=ADMIN)
    assert other.status_code == 200 and "options" not in other.json()
