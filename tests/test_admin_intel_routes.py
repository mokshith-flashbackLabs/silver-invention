"""``/v1/admin/intel/*`` — the step-1 admin surface (spec §4.7, Task 12).

Fakes stand in for :class:`IntelStore` and :class:`EvidenceStore`. Both tokens'
presence is covered generically by ``tests/test_route_auth_coverage.py``; this
file is about shape, refusals, and the two checks that need the STORED row
rather than the request body alone (tag membership by diff, and ``query_text``
against the source's own ``kind``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from imageshield.intel.models import Run, Source, SpendToday, Vocabulary
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config

ADMIN = {"X-Service-Token": SERVICE_TOKEN, "X-Admin-Service-Token": ADMIN_SERVICE_TOKEN}


def _now() -> datetime:
    return datetime.now(UTC)


class FakeIntelStore:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.known_hits: set[str] = set()
        self.vocab_applied = True
        self.registry_tags = [
            {"slug": "instagram", "label": "Instagram", "description": "", "retired": False},
            {"slug": "vine", "label": "Vine", "description": "", "retired": True},
        ]
        self.sources: dict[UUID, Source] = {}
        self.runs: list[Run] = []
        self.checked: list[UUID] = []
        self.adhoc: list[str] = []
        self.spend_call_count = 0
        self.spend_cost_usd = Decimal("0")
        self.spend_budget_usd: Decimal | None = None

    async def create_source(self, **kwargs: Any) -> Source:
        self.created.append(kwargs)
        now = _now()
        source = Source(
            source_id=uuid4(),
            kind=kwargs["kind"],
            source_url=kwargs.get("source_url"),
            url_hash=None,
            query_text=kwargs.get("query_text"),
            tags=tuple(kwargs.get("tags", ())),
            check_every_hours=kwargs["check_every_hours"],
            next_check_at=now,
            enabled=True,
            terms_note=kwargs["terms_note"],
            last_content_sha256=None,
            last_checked_at=None,
            last_run_status=None,
            consecutive_failures=0,
            disabled_reason=None,
            created_by=kwargs["operator"],
            created_at=now,
        )
        self.sources[source.source_id] = source
        return source

    async def list_sources(
        self, *, cursor: tuple[datetime, UUID] | None, limit: int
    ) -> list[Source]:
        rows = sorted(
            self.sources.values(), key=lambda s: (s.created_at, s.source_id), reverse=True
        )
        return rows[:limit]

    async def get_source(self, source_id: UUID) -> Source | None:
        return self.sources.get(source_id)

    async def patch_source(self, source_id: UUID, *, operator: str, **fields: Any) -> Source | None:
        existing = self.sources.get(source_id)
        if existing is None:
            return None
        updates = {key: value for key, value in fields.items() if value is not None}
        updated = existing.model_copy(update=updates)
        self.sources[source_id] = updated
        return updated

    async def is_known_hit(self, value: str) -> bool:
        return value in self.known_hits

    async def load_vocabulary(self) -> Vocabulary:
        return Vocabulary(
            release_no=1,
            map_version=1,
            scoring_version="s",
            quiz_version="q",
            document={"tags": self.registry_tags},
        )

    async def queue_source_check(self, source_id: UUID, *, operator: str) -> UUID | None:
        if source_id not in self.sources:
            return None
        run_id = uuid4()
        self.checked.append(source_id)
        return run_id

    async def queue_adhoc(self, url: str, *, operator: str) -> UUID:
        self.adhoc.append(url)
        return uuid4()

    async def list_runs(self, *, cursor: tuple[datetime, UUID] | None, limit: int) -> list[Run]:
        return self.runs[:limit]

    async def put_vocabulary(self, **kwargs: Any) -> bool:
        return self.vocab_applied

    async def spend_today(self, now: datetime) -> SpendToday:
        return SpendToday(
            spend_date=now.date(),
            call_count=self.spend_call_count,
            spent_today_usd=self.spend_cost_usd,
            daily_budget_usd=self.spend_budget_usd,
        )


def _signal_row(signal_id: UUID, *, status: str = "active") -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "document_id": uuid4(),
        "category": "policy",
        "direction": "risk_up",
        "tags": ["instagram"],
        "unregistered_subjects": [],
        "summary": "a policy changed",
        "model_id": "claude-intel",
        "prompt_version": "v1",
        "status": status,
        "retracted_by": None,
        "retracted_at": None,
        "retract_reason": None,
        "created_at": _now(),
    }


class FakeEvidenceStore:
    def __init__(self) -> None:
        self.signals: dict[UUID, dict[str, Any]] = {}

    def seed(self, signal_id: UUID, *, status: str = "active") -> dict[str, Any]:
        row = _signal_row(signal_id, status=status)
        self.signals[signal_id] = row
        return row

    async def list_signals(
        self, *, cursor: tuple[datetime, UUID] | None, limit: int
    ) -> list[dict[str, Any]]:
        rows = sorted(
            self.signals.values(), key=lambda r: (r["created_at"], r["signal_id"]), reverse=True
        )
        return rows[:limit]

    async def get_signal(self, signal_id: UUID) -> dict[str, Any] | None:
        row = self.signals.get(signal_id)
        if row is None:
            return None
        return {**row, "excerpts": [], "document": None}

    async def retract_signal(self, signal_id: UUID, *, operator: str, reason: str) -> str:
        row = self.signals.get(signal_id)
        if row is None:
            return "not_found"
        if row["status"] != "active":
            return "not_active"
        row["status"] = "retracted"
        row["retracted_by"] = operator
        row["retract_reason"] = reason
        return "retracted"


def _client() -> tuple[TestClient, FakeIntelStore]:
    app = create_app(config=make_config())
    store = FakeIntelStore()
    app.state.intel_store = store
    app.state.evidence_store = FakeEvidenceStore()
    return TestClient(app), store


def _source(**kw: Any) -> dict[str, Any]:
    body = {
        "kind": "policy_page",
        "source_url": "https://p.example/terms",
        "tags": ["instagram"],
        "check_every_hours": 24,
        "terms_note": "automated access permitted",
        "operator": "alice",
    }
    body.update(kw)
    return body


def test_create_source_validates_tags_by_diff() -> None:
    client, _ = _client()
    created = client.post("/v1/admin/intel/sources", json=_source(), headers=ADMIN)
    assert created.status_code == 201
    r = client.post("/v1/admin/intel/sources", json=_source(tags=["bumble"]), headers=ADMIN)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_tag"
    r = client.post("/v1/admin/intel/sources", json=_source(tags=["vine"]), headers=ADMIN)
    assert r.status_code == 422 and r.json()["error"]["code"] == "tag_retired"
    r = client.post("/v1/admin/intel/sources", json=_source(tags=["Not-A-Slug"]), headers=ADMIN)
    assert r.status_code == 422


def test_a_query_naming_a_person_is_refused() -> None:
    client, _ = _client()
    body = _source(
        kind="search_query", source_url=None, query_text="leaks mentioning jane@example.com"
    )
    r = client.post("/v1/admin/intel/sources", json=body, headers=ADMIN)
    assert r.status_code == 422 and r.json()["error"]["code"] == "query_names_a_person"


def test_a_known_hit_location_is_refused() -> None:
    from imageshield.search.urlhash import url_hash

    client, store = _client()
    store.known_hits.add(url_hash("https://abuse.example/x"))
    r = client.post(
        "/v1/admin/intel/documents",
        json={"url": "https://abuse.example/x", "operator": "alice"},
        headers=ADMIN,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "known_hit_location"


def test_document_is_queued_when_not_a_known_hit() -> None:
    client, store = _client()
    r = client.post(
        "/v1/admin/intel/documents",
        json={"url": "https://news.example/story", "operator": "alice"},
        headers=ADMIN,
    )
    assert r.status_code == 202
    assert UUID(r.json()["run_id"])
    assert store.adhoc == ["https://news.example/story"]


def test_check_on_an_unknown_source_is_404() -> None:
    client, _ = _client()
    r = client.post(
        f"/v1/admin/intel/sources/{uuid4()}/check", json={"operator": "a"}, headers=ADMIN
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "intel_source_not_found"


def test_check_on_a_real_source_queues_a_run() -> None:
    client, store = _client()
    created = client.post("/v1/admin/intel/sources", json=_source(), headers=ADMIN).json()
    r = client.post(
        f"/v1/admin/intel/sources/{created['source_id']}/check",
        json={"operator": "alice"},
        headers=ADMIN,
    )
    assert r.status_code == 200
    assert UUID(r.json()["run_id"])
    assert store.checked == [UUID(created["source_id"])]


def test_patch_on_an_unknown_source_is_404() -> None:
    client, _ = _client()
    r = client.patch(f"/v1/admin/intel/sources/{uuid4()}", json={"operator": "a"}, headers=ADMIN)
    assert r.status_code == 404 and r.json()["error"]["code"] == "intel_source_not_found"


def test_patch_source_refuses_query_text_on_a_non_search_query_source() -> None:
    client, _ = _client()
    created = client.post("/v1/admin/intel/sources", json=_source(), headers=ADMIN).json()
    r = client.patch(
        f"/v1/admin/intel/sources/{created['source_id']}",
        json={"query_text": "site:example.com leaks", "operator": "alice"},
        headers=ADMIN,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "query_text_wrong_kind"


def test_patch_source_refuses_a_query_text_naming_a_person() -> None:
    client, _ = _client()
    created = client.post(
        "/v1/admin/intel/sources",
        json=_source(kind="search_query", source_url=None, query_text="leaks", tags=[]),
        headers=ADMIN,
    ).json()
    r = client.patch(
        f"/v1/admin/intel/sources/{created['source_id']}",
        json={"query_text": "leaks mentioning jane@example.com", "operator": "alice"},
        headers=ADMIN,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "query_names_a_person"


def test_patch_source_validates_tags_by_diff() -> None:
    client, _ = _client()
    created = client.post("/v1/admin/intel/sources", json=_source(), headers=ADMIN).json()
    source_id = created["source_id"]
    r = client.patch(
        f"/v1/admin/intel/sources/{source_id}",
        json={"tags": ["instagram", "bumble"], "operator": "alice"},
        headers=ADMIN,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_tag"
    r = client.patch(
        f"/v1/admin/intel/sources/{source_id}",
        json={"tags": ["vine"], "operator": "alice"},
        headers=ADMIN,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "tag_retired"
    r = client.patch(
        f"/v1/admin/intel/sources/{source_id}",
        json={"tags": ["Not-A-Slug"], "operator": "alice"},
        headers=ADMIN,
    )
    assert r.status_code == 422


def test_patch_source_keeping_its_own_retired_tag_is_not_a_new_addition() -> None:
    """A tag already on the source's own target is not "added" (spec §3.1) —
    only the diff against the stored row reaches ``_check_tags``. A source can
    only carry a retired tag this way (retired AFTER attachment), since create
    checks its whole tag set with nothing to diff against — so this seeds the
    stored row directly rather than through the create route."""
    client, store = _client()
    created = client.post("/v1/admin/intel/sources", json=_source(), headers=ADMIN).json()
    source_id = UUID(created["source_id"])
    existing = store.sources[source_id]
    store.sources[source_id] = existing.model_copy(update={"tags": ("instagram", "vine")})

    r = client.patch(
        f"/v1/admin/intel/sources/{source_id}",
        json={"tags": ["instagram", "vine"], "check_every_hours": 48, "operator": "alice"},
        headers=ADMIN,
    )
    assert r.status_code == 200
    assert r.json()["check_every_hours"] == 48


def test_patch_source_updates_and_returns_the_source() -> None:
    client, _ = _client()
    created = client.post("/v1/admin/intel/sources", json=_source(), headers=ADMIN).json()
    r = client.patch(
        f"/v1/admin/intel/sources/{created['source_id']}",
        json={"enabled": False, "operator": "alice"},
        headers=ADMIN,
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_list_sources_reports_a_next_cursor_only_at_the_page_limit() -> None:
    client, _ = _client()
    client.post("/v1/admin/intel/sources", json=_source(), headers=ADMIN)
    client.post(
        "/v1/admin/intel/sources",
        json=_source(source_url="https://p.example/other"),
        headers=ADMIN,
    )

    full = client.get("/v1/admin/intel/sources", params={"limit": 1}, headers=ADMIN)
    assert full.status_code == 200
    assert len(full.json()["sources"]) == 1
    assert full.json()["next_cursor"] is not None

    partial = client.get("/v1/admin/intel/sources", params={"limit": 50}, headers=ADMIN)
    assert len(partial.json()["sources"]) == 2
    assert partial.json()["next_cursor"] is None


def test_list_sources_rejects_a_malformed_cursor() -> None:
    client, _ = _client()
    r = client.get("/v1/admin/intel/sources", params={"cursor": "not-a-cursor"}, headers=ADMIN)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_cursor"


def test_list_runs_reports_spend() -> None:
    client, store = _client()
    store.spend_call_count = 4
    store.spend_cost_usd = Decimal("2.00")
    store.spend_budget_usd = Decimal("50.00")

    r = client.get("/v1/admin/intel/runs", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["runs"] == []
    assert body["next_cursor"] is None
    assert body["spend"] == {
        "spend_date": body["spend"]["spend_date"],
        "call_count": 4,
        "spent_today_usd": "2.00",
        "daily_budget_usd": "50.00",
        "budget_headroom_usd": "48.00",
    }


def test_list_runs_reports_no_headroom_when_no_budget_is_set() -> None:
    client, _ = _client()
    r = client.get("/v1/admin/intel/runs", headers=ADMIN)
    assert r.json()["spend"]["daily_budget_usd"] is None
    assert r.json()["spend"]["budget_headroom_usd"] is None


def test_list_signals_and_get_signal() -> None:
    client, _ = _client()
    evidence: FakeEvidenceStore = client.app.state.evidence_store
    signal_id = uuid4()
    evidence.seed(signal_id)

    listed = client.get("/v1/admin/intel/signals", headers=ADMIN)
    assert listed.status_code == 200
    assert [s["signal_id"] for s in listed.json()["signals"]] == [str(signal_id)]

    got = client.get(f"/v1/admin/intel/signals/{signal_id}", headers=ADMIN)
    assert got.status_code == 200
    assert got.json()["excerpts"] == []
    assert got.json()["document"] is None


def test_get_an_unknown_signal_is_404() -> None:
    client, _ = _client()
    r = client.get(f"/v1/admin/intel/signals/{uuid4()}", headers=ADMIN)
    assert r.status_code == 404 and r.json()["error"]["code"] == "signal_not_found"


def test_retract_signal_transitions() -> None:
    client, _ = _client()
    evidence: FakeEvidenceStore = client.app.state.evidence_store
    signal_id = uuid4()
    evidence.seed(signal_id)

    r = client.post(
        f"/v1/admin/intel/signals/{signal_id}/retract",
        json={"reason": "not credible", "operator": "alice"},
        headers=ADMIN,
    )
    assert r.status_code == 200 and r.json() == {"status": "retracted"}

    again = client.post(
        f"/v1/admin/intel/signals/{signal_id}/retract",
        json={"reason": "not credible", "operator": "alice"},
        headers=ADMIN,
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "signal_not_active"

    missing = client.post(
        f"/v1/admin/intel/signals/{uuid4()}/retract",
        json={"reason": "not credible", "operator": "alice"},
        headers=ADMIN,
    )
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "signal_not_found"


def test_vocabulary_push_takes_no_operator_and_is_idempotent() -> None:
    client, store = _client()
    body = {
        "release_no": 3,
        "map_version": 2,
        "scoring_version": "s3",
        "quiz_version": "q3",
        "document": {"tags": [], "questions": [], "option_tags": [], "renamed": []},
    }
    r = client.put("/v1/admin/intel/vocabulary", json=body, headers=ADMIN)
    assert r.status_code == 200 and r.json() == {"applied": True}
    store.vocab_applied = False
    assert client.put("/v1/admin/intel/vocabulary", json=body, headers=ADMIN).json() == {
        "applied": False
    }
    r = client.put("/v1/admin/intel/vocabulary", json={**body, "operator": "x"}, headers=ADMIN)
    assert r.status_code == 422  # extra='forbid': the system write carries no operator


def test_vocabulary_document_rejects_a_malformed_tag() -> None:
    client, _ = _client()
    body = {
        "release_no": 4,
        "map_version": 3,
        "scoring_version": "s4",
        "quiz_version": "q4",
        "document": {
            "tags": [{"slug": "Not-Lower", "label": "x", "kind": "platform", "retired": False}],
            "questions": [],
            "option_tags": [],
            "renamed": [],
        },
    }
    r = client.put("/v1/admin/intel/vocabulary", json=body, headers=ADMIN)
    assert r.status_code == 422


def test_vocabulary_document_rejects_an_unknown_top_level_key() -> None:
    client, _ = _client()
    body = {
        "release_no": 5,
        "map_version": 4,
        "scoring_version": "s5",
        "quiz_version": "q5",
        "document": {
            "tags": [],
            "questions": [],
            "option_tags": [],
            "renamed": [],
            "bogus": True,
        },
    }
    r = client.put("/v1/admin/intel/vocabulary", json=body, headers=ADMIN)
    assert r.status_code == 422
