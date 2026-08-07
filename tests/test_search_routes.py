"""Seeds + search endpoints — route behaviour over an in-memory fake store
(repo convention: TestClient never runs the lifespan; the real Postgres
store is tested in tests/test_search_store.py).

Pinned behaviours:

- every route requires X-Service-Token;
- POST /v1/search is 202 and only enqueues (the fake records create_run —
  dispatch is the worker's job, never the request handler's);
- a seed belonging to another user is indistinguishable from a missing one
  (404, no cross-user probing);
- unknown providers are a 422, before any run row exists;
- run status keeps providers_attempted and providers_succeeded distinct;
- match serialisation carries score_kind plus one numeric and one
  categorical row faithfully.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from imageshield.search.models import MatchRow, ProviderDescriptor, RunRow, SeedRow
from imageshield.search.provider import ProviderMatch, ProviderResult
from imageshield.types import ProviderId, UserRef
from tests.conftest import SERVICE_TOKEN, make_config

AUTH = {"X-Service-Token": SERVICE_TOKEN}


class FakeSearchStore:
    def __init__(self) -> None:
        self.seeds: dict[UUID, SeedRow] = {}
        self.runs: dict[UUID, RunRow] = {}
        self.matches: list[MatchRow] = []
        self.created_runs: list[tuple[UserRef, UUID, tuple[ProviderId, ...]]] = []
        self.enabled: tuple[ProviderId, ...] = (ProviderId("google"), ProviderId("hive"))

    async def create_seed(
        self, user_ref: UserRef, seed_kind: str, source_object_uri: str
    ) -> UUID:
        seed_id = uuid4()
        self.seeds[seed_id] = SeedRow(
            seed_id=seed_id,
            user_ref=user_ref,
            seed_kind=seed_kind,
            source_object_uri=source_object_uri,
            status="active",
            created_at=datetime.now(UTC),
        )
        return seed_id

    async def get_seed(self, seed_id: UUID) -> SeedRow | None:
        return self.seeds.get(seed_id)

    async def create_run(
        self, user_ref: UserRef, seed_id: UUID, providers_attempted: Sequence[ProviderId]
    ) -> UUID:
        run_id = uuid4()
        self.created_runs.append((user_ref, seed_id, tuple(providers_attempted)))
        return run_id

    async def get_run(self, run_id: UUID) -> RunRow | None:
        return self.runs.get(run_id)

    async def claim_run(self, run_id: UUID) -> Any:
        raise NotImplementedError

    async def enabled_provider_ids(self) -> tuple[ProviderId, ...]:
        return self.enabled

    async def record_provider_call(self, run_id: UUID, result: ProviderResult) -> None:
        raise NotImplementedError

    async def record_matches(
        self,
        run_id: UUID,
        user_ref: UserRef,
        provider: ProviderDescriptor,
        matches: Sequence[ProviderMatch],
    ) -> int:
        raise NotImplementedError

    async def complete_run(
        self, run_id: UUID, providers_succeeded: Sequence[ProviderId]
    ) -> None:
        raise NotImplementedError

    async def list_matches(
        self, user_ref: UserRef, since: datetime | None
    ) -> tuple[MatchRow, ...]:
        return tuple(self.matches)


def make_client() -> tuple[TestClient, FakeSearchStore]:
    app = create_app(config=make_config())
    store = FakeSearchStore()
    app.state.search_store = store
    return TestClient(app), store


def _seed_body(user_ref: UUID) -> dict[str, Any]:
    return {
        "user_ref": str(user_ref),
        "seed_kind": "user_supplied",
        "source_object_uri": "https://proxy-s3.example/seed.jpg?sig=abc",
    }


def test_all_routes_require_service_token() -> None:
    client, _ = make_client()
    assert client.post("/v1/seeds", json={}).status_code == 401
    assert client.post("/v1/search", json={}).status_code == 401
    assert client.get(f"/v1/search/runs/{uuid4()}").status_code == 401
    assert client.get(f"/v1/search/matches?user_ref={uuid4()}").status_code == 401


def test_create_seed_201() -> None:
    client, store = make_client()
    user_ref = uuid4()

    response = client.post("/v1/seeds", json=_seed_body(user_ref), headers=AUTH)

    assert response.status_code == 201
    seed_id = UUID(response.json()["seed_id"])
    assert store.seeds[seed_id].user_ref == user_ref
    assert store.seeds[seed_id].seed_kind == "user_supplied"


def test_create_seed_rejects_non_http_uri_and_bad_kind() -> None:
    client, _ = make_client()
    body = _seed_body(uuid4())
    body["source_object_uri"] = "s3://bucket/key.jpg"  # we hold no S3 creds
    assert client.post("/v1/seeds", json=body, headers=AUTH).status_code == 422

    body = _seed_body(uuid4())
    body["seed_kind"] = "scraped"
    assert client.post("/v1/seeds", json=body, headers=AUTH).status_code == 422


def test_create_search_202_defaults_to_all_enabled_providers() -> None:
    client, store = make_client()
    user_ref = uuid4()
    seed = client.post("/v1/seeds", json=_seed_body(user_ref), headers=AUTH).json()

    response = client.post(
        "/v1/search",
        json={"user_ref": str(user_ref), "seed_id": seed["seed_id"]},
        headers=AUTH,
    )

    assert response.status_code == 202
    assert UUID(response.json()["run_id"])
    [(run_user, run_seed, providers)] = store.created_runs
    assert run_user == user_ref
    assert run_seed == UUID(seed["seed_id"])
    assert providers == ("google", "hive")


def test_create_search_subset_and_unknown_provider() -> None:
    client, store = make_client()
    user_ref = uuid4()
    seed = client.post("/v1/seeds", json=_seed_body(user_ref), headers=AUTH).json()

    ok = client.post(
        "/v1/search",
        json={"user_ref": str(user_ref), "seed_id": seed["seed_id"], "providers": ["hive"]},
        headers=AUTH,
    )
    assert ok.status_code == 202
    assert store.created_runs[-1][2] == ("hive",)

    bad = client.post(
        "/v1/search",
        json={
            "user_ref": str(user_ref),
            "seed_id": seed["seed_id"],
            "providers": ["hive", "pimeyes"],
        },
        headers=AUTH,
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "unknown_provider"
    assert len(store.created_runs) == 1  # no run row for the rejected request


def test_create_search_wrong_user_or_missing_seed_is_404() -> None:
    client, store = make_client()
    owner, intruder = uuid4(), uuid4()
    seed = client.post("/v1/seeds", json=_seed_body(owner), headers=AUTH).json()

    stolen = client.post(
        "/v1/search",
        json={"user_ref": str(intruder), "seed_id": seed["seed_id"]},
        headers=AUTH,
    )
    missing = client.post(
        "/v1/search",
        json={"user_ref": str(owner), "seed_id": str(uuid4())},
        headers=AUTH,
    )

    assert stolen.status_code == 404
    assert missing.status_code == 404
    # not-yours must be indistinguishable from not-exists
    assert stolen.json()["error"]["code"] == missing.json()["error"]["code"] == "seed_not_found"
    assert store.created_runs == []


def test_run_status_keeps_attempted_and_succeeded_distinct() -> None:
    client, store = make_client()
    run_id = uuid4()
    store.runs[run_id] = RunRow(
        run_id=run_id,
        seed_id=uuid4(),
        user_ref=UserRef(uuid4()),
        status="completed",
        providers_attempted=("hive", "google"),
        providers_succeeded=("hive",),
        matches_found=3,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )

    response = client.get(f"/v1/search/runs/{run_id}", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "completed",
        "providers_attempted": ["hive", "google"],
        "providers_succeeded": ["hive"],
        "matches_found": 3,
    }

    assert client.get(f"/v1/search/runs/{uuid4()}", headers=AUTH).status_code == 404


def test_matches_serialise_both_score_shapes() -> None:
    client, store = make_client()
    run_id, user_ref = uuid4(), uuid4()
    now = datetime.now(UTC)
    store.matches = [
        MatchRow(
            match_id=uuid4(),
            run_id=run_id,
            provider_id="hive",
            image_url="https://x/a.jpg",
            page_url="https://page/a",
            score_kind="numeric",
            provider_score=Decimal("0.8712"),
            provider_category=None,
            query_quality="good",
            band="review",
            created_at=now,
        ),
        MatchRow(
            match_id=uuid4(),
            run_id=run_id,
            provider_id="google",
            image_url="https://x/a.jpg",
            page_url=None,
            score_kind="categorical",
            provider_score=None,
            provider_category="full_match",
            query_quality=None,
            band="review",
            created_at=now,
        ),
    ]

    response = client.get(f"/v1/search/matches?user_ref={user_ref}", headers=AUTH)

    assert response.status_code == 200
    numeric, categorical = response.json()["matches"]
    assert numeric["score_kind"] == "numeric"
    assert numeric["provider_score"] == 0.8712
    assert numeric["provider_category"] is None
    assert numeric["band"] == "review"
    assert categorical["score_kind"] == "categorical"
    assert categorical["provider_score"] is None
    assert categorical["provider_category"] == "full_match"
