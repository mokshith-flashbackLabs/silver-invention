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

import pytest
from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from imageshield.search.feedback import status_for
from imageshield.search.models import (
    AttestationRow,
    InfringementRow,
    ProviderDescriptor,
    RunRow,
    SeedRow,
)
from imageshield.search.provider import ProviderMatch
from imageshield.search.store import UnknownSubject
from imageshield.subjects.eligibility import eligibility_for
from imageshield.subjects.models import Eligibility, SubjectRow
from imageshield.types import ProviderId, UserRef
from tests.conftest import SERVICE_TOKEN, make_config

AUTH = {"X-Service-Token": SERVICE_TOKEN}


class FakeSearchStore:
    def __init__(self) -> None:
        self.seeds: dict[UUID, SeedRow] = {}
        self.runs: dict[UUID, RunRow] = {}
        self.infringements: list[InfringementRow] = []
        self.created_runs: list[
            tuple[UserRef, UUID, tuple[ProviderId, ...], str]
        ] = []
        self.enabled: tuple[ProviderId, ...] = (ProviderId("google"), ProviderId("hive"))
        # Models migration 0008's search_seeds_subject_fk. Without this the fake
        # accepted a seed for an unparented user_ref and returned 201, while the
        # real Postgres raised ForeignKeyViolation and the route answered a bare
        # 500 — a divergence that hid the defect from the whole route suite.
        self.known_subjects: set[UserRef] = set()
        self.feedback: list[tuple[UUID, UserRef, str]] = []
        self.owners: dict[UUID, set[UserRef]] = {}

    async def create_seed(
        self, user_ref: UserRef, seed_kind: str, source_object_ref: str
    ) -> UUID:
        if self.known_subjects and user_ref not in self.known_subjects:
            raise UnknownSubject("no subjects row for this user_ref")
        seed_id = uuid4()
        self.seeds[seed_id] = SeedRow(
            seed_id=seed_id,
            user_ref=user_ref,
            seed_kind=seed_kind,
            source_object_ref=source_object_ref,
            status="active",
            created_at=datetime.now(UTC),
        )
        return seed_id

    async def get_seed(self, seed_id: UUID) -> SeedRow | None:
        return self.seeds.get(seed_id)

    async def create_run(
        self,
        user_ref: UserRef,
        seed_id: UUID,
        providers_attempted: Sequence[ProviderId],
        *,
        seed_url: str,
    ) -> UUID:
        run_id = uuid4()
        self.created_runs.append(
            (user_ref, seed_id, tuple(providers_attempted), seed_url)
        )
        return run_id

    async def get_run(self, run_id: UUID) -> RunRow | None:
        return self.runs.get(run_id)

    async def claim_run(self, run_id: UUID) -> Any:
        raise NotImplementedError

    async def enabled_provider_ids(self) -> tuple[ProviderId, ...]:
        return self.enabled

    async def update_cadence(self, seed_id: UUID, update: Any) -> None:
        raise NotImplementedError

    async def record_infringements(
        self,
        run_id: UUID,
        user_ref: UserRef,
        provider: ProviderDescriptor,
        matches: Sequence[ProviderMatch],
        policy: Any,
    ) -> int:
        raise NotImplementedError

    async def complete_run(
        self, run_id: UUID, providers_succeeded: Sequence[ProviderId]
    ) -> None:
        raise NotImplementedError

    async def list_infringements(
        self, user_ref: UserRef, since: datetime | None
    ) -> tuple[InfringementRow, ...]:
        return tuple(self.infringements)

    async def record_feedback(
        self, infringement_id: UUID, user_ref: UserRef, signal: str
    ) -> str | None:
        # Ownership is part of the LOOKUP, exactly as in the real store's WHERE
        # clause: a foreign row and a missing row are the same miss, so the
        # route is never handed a difference it could leak.
        match = next(
            (
                row
                for row in self.infringements
                if row.infringement_id == infringement_id
            ),
            None,
        )
        if match is None or user_ref not in self.owners.get(infringement_id, set()):
            return None
        self.feedback.append((infringement_id, user_ref, signal))
        status = status_for(signal)
        return match.status if status is None else status


class FakeSubjectStore:
    """Records every refusal, so "exactly one audit row and nothing else" is
    assertable without a database."""

    def __init__(self) -> None:
        self.subjects: dict[UserRef, Eligibility] = {}
        self.refusals: list[tuple[UserRef, str, dict[str, Any]]] = []

    async def get_subject(self, user_ref: UserRef) -> SubjectRow | None:
        eligibility = self.subjects.get(user_ref)
        if eligibility is None:
            return None
        now = datetime.now(UTC)
        return SubjectRow(
            user_ref=user_ref,
            discovery_eligible=eligibility.discovery_eligible,
            eligibility_reason=eligibility.eligibility_reason,
            created_at=now,
            updated_at=now,
        )

    async def upsert_subject(self, user_ref: UserRef, eligibility: Eligibility) -> None:
        self.subjects[user_ref] = eligibility

    async def record_discovery_refusal(
        self, user_ref: UserRef, *, outcome: str, metadata: Any
    ) -> None:
        self.refusals.append((user_ref, outcome, dict(metadata)))


def make_client() -> tuple[TestClient, FakeSearchStore, FakeSubjectStore]:
    app = create_app(config=make_config())
    store = FakeSearchStore()
    subjects = FakeSubjectStore()
    app.state.search_store = store
    app.state.subject_store = subjects
    return TestClient(app), store, subjects


def _enrolled_adult(subjects: FakeSubjectStore, user_ref: UUID) -> None:
    subjects.subjects[UserRef(user_ref)] = eligibility_for(True)


# An opaque durable object key, NOT a URL. A presigned URL arriving here is
# the exact bug 0011 fixes, so the validator refuses it (see the 422 tests).
SEED_REF = "seeds/2026/08/9f1c1e6a.jpg"
# A freshly-minted presigned GET, supplied per run by the proxy. Lives on the
# RUN, never on the seed.
SEED_URL = "https://proxy-s3.example/seed.jpg?X-Amz-Signature=abc"


def _seed_body(user_ref: UUID) -> dict[str, Any]:
    return {
        "user_ref": str(user_ref),
        "seed_kind": "user_supplied",
        "source_object_ref": SEED_REF,
    }


def _search_body(
    user_ref: UUID | str, seed_id: UUID | str, **extra: Any
) -> dict[str, Any]:
    return {
        "user_ref": str(user_ref),
        "seed_id": str(seed_id),
        "seed_url": SEED_URL,
        **extra,
    }


def test_all_routes_require_service_token() -> None:
    client, _, _subjects = make_client()
    assert client.post("/v1/seeds", json={}).status_code == 401
    assert client.post("/v1/search", json={}).status_code == 401
    assert client.get(f"/v1/search/runs/{uuid4()}").status_code == 401
    assert client.get(f"/v1/search/infringements?user_ref={uuid4()}").status_code == 401


def test_create_seed_201() -> None:
    client, store, _subjects = make_client()
    user_ref = uuid4()

    response = client.post("/v1/seeds", json=_seed_body(user_ref), headers=AUTH)

    assert response.status_code == 201
    seed_id = UUID(response.json()["seed_id"])
    assert store.seeds[seed_id].user_ref == user_ref
    assert store.seeds[seed_id].seed_kind == "user_supplied"


@pytest.mark.parametrize(
    "ref",
    [
        "https://proxy-s3.example/seed.jpg?X-Amz-Signature=deadbeef",
        "https://proxy-s3.example/seed.jpg",
        "http://proxy-s3.example/seed.jpg",
        "HTTPS://PROXY-S3.EXAMPLE/seed.jpg",
        "seeds/abc.jpg?x-amz-signature=deadbeef",
    ],
)
def test_create_seed_rejects_a_presigned_url_with_422(ref: str) -> None:
    """Done-when: POST /v1/seeds REJECTS an https:// value, asserted with a real
    presigned-URL-shaped string.

    The validator is INVERTED from what it was -- it used to *require* https://.
    A presigned URL is a credential with days of life; stored on a seed it 403s
    forever from week two and presents as a provider outage rather than as our
    URLs expiring. It arriving here is the exact bug 0011 fixes, so it has to
    fail loudly rather than be accepted and quietly rot.
    """
    client, store, _subjects = make_client()
    body = _seed_body(uuid4())
    body["source_object_ref"] = ref

    response = client.post("/v1/seeds", json=body, headers=AUTH)

    assert response.status_code == 422
    assert store.seeds == {}


@pytest.mark.parametrize(
    "ref", ["seeds/2026/08/abc.jpg", "s3://bucket/key.jpg", "abc123"]
)
def test_create_seed_accepts_an_opaque_durable_ref(ref: str) -> None:
    """Anything that is not an http(s) URL and carries no signature.

    `s3://` passes now, where it used to be refused for "we hold no S3 creds".
    That reason no longer applies: we never dereference the ref at all -- the
    proxy resolves it and mints the presigned GET. Only the *expiring* shapes
    are refused.
    """
    client, store, subjects = make_client()
    user_ref = uuid4()
    _enrolled_adult(subjects, user_ref)
    body = _seed_body(user_ref)
    body["source_object_ref"] = ref

    response = client.post("/v1/seeds", json=body, headers=AUTH)

    assert response.status_code == 201
    assert store.seeds[UUID(response.json()["seed_id"])].source_object_ref == ref


def test_create_seed_rejects_a_bad_kind() -> None:
    client, _, _subjects = make_client()
    body = _seed_body(uuid4())
    body["seed_kind"] = "scraped"
    assert client.post("/v1/seeds", json=body, headers=AUTH).status_code == 422


def test_create_search_202_defaults_to_all_enabled_providers() -> None:
    client, store, subjects = make_client()
    user_ref = uuid4()
    _enrolled_adult(subjects, user_ref)
    seed = client.post("/v1/seeds", json=_seed_body(user_ref), headers=AUTH).json()

    response = client.post(
        "/v1/search",
        json=_search_body(user_ref, seed["seed_id"]),
        headers=AUTH,
    )

    assert response.status_code == 202
    assert UUID(response.json()["run_id"])
    [(run_user, run_seed, providers, run_seed_url)] = store.created_runs
    assert run_user == user_ref
    assert run_seed == UUID(seed["seed_id"])
    assert providers == ("google", "hive")
    # The presigned URL is stored on the RUN. The seed keeps only its opaque
    # ref, which is what stops it expiring a week after creation.
    assert run_seed_url == SEED_URL
    assert store.seeds[run_seed].source_object_ref == SEED_REF


def test_create_search_subset_and_unknown_provider() -> None:
    client, store, subjects = make_client()
    user_ref = uuid4()
    _enrolled_adult(subjects, user_ref)
    seed = client.post("/v1/seeds", json=_seed_body(user_ref), headers=AUTH).json()

    ok = client.post(
        "/v1/search",
        json=_search_body(user_ref, seed["seed_id"], providers=["hive"]),
        headers=AUTH,
    )
    assert ok.status_code == 202
    assert store.created_runs[-1][2] == ("hive",)

    bad = client.post(
        "/v1/search",
        json=_search_body(
            user_ref, seed["seed_id"], providers=["hive", "pimeyes"]
        ),
        headers=AUTH,
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "unknown_provider"
    assert len(store.created_runs) == 1  # no run row for the rejected request


def test_create_search_wrong_user_or_missing_seed_is_404() -> None:
    client, store, subjects = make_client()
    owner, intruder = uuid4(), uuid4()
    _enrolled_adult(subjects, owner)
    _enrolled_adult(subjects, intruder)
    seed = client.post("/v1/seeds", json=_seed_body(owner), headers=AUTH).json()

    stolen = client.post(
        "/v1/search",
        json=_search_body(intruder, seed["seed_id"]),
        headers=AUTH,
    )
    missing = client.post(
        "/v1/search",
        json=_search_body(owner, uuid4()),
        headers=AUTH,
    )

    assert stolen.status_code == 404
    assert missing.status_code == 404
    # not-yours must be indistinguishable from not-exists
    assert stolen.json()["error"]["code"] == missing.json()["error"]["code"] == "seed_not_found"
    assert store.created_runs == []


def test_run_status_keeps_attempted_and_succeeded_distinct() -> None:
    client, store, _subjects = make_client()
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
        scan_tier="relaxed",
        next_scan_after=datetime(2026, 8, 23, tzinfo=UTC),
    )

    response = client.get(f"/v1/search/runs/{run_id}", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "completed",
        "providers_attempted": ["hive", "google"],
        "providers_succeeded": ["hive"],
        "matches_found": 3,
        # Tiering must never be silent: the proxy reads the real cadence off
        # this response so it can tell the user the truth about it.
        "scan_tier": "relaxed",
        "next_scan_after": "2026-08-23T00:00:00Z",
    }

    assert client.get(f"/v1/search/runs/{uuid4()}", headers=AUTH).status_code == 404


# ── Step 8: the eligibility guard ─────────────────────────────────────────


def test_a_seed_for_an_unenrolled_subject_is_409_not_a_bare_500() -> None:
    """Migration 0008's FK made this request fail for the first time, and the
    unhandled psycopg exception came out as a plain-text 500 with none of the
    error envelope — no `code`, no `retryable`, no `request_id`.

    A 5xx reads as transient, so the proxy would retry a request that can never
    succeed, and no client-facing copy could be selected. 409 with the same
    `subject_unknown` code POST /v1/search already returns for the identical
    condition is the terminal, machine-readable answer.
    """
    client, store, _ = make_client()
    store.known_subjects = {UserRef(uuid4())}  # somebody else
    stranger = uuid4()

    response = client.post("/v1/seeds", json=_seed_body(stranger), headers=AUTH)

    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "subject_unknown"
    assert body["retryable"] is False
    assert store.seeds == {}


def test_a_minor_may_still_hold_a_seed() -> None:
    """Only discovery is refused for a minor, not enrolment or seed-holding.

    Gating /v1/seeds on eligibility as well would break the enrolment flow for a
    user group v1 deliberately supports — consent, guardianship and household
    seats all work. The refusal belongs where the searching happens.
    """
    client, store, subjects = make_client()
    minor = uuid4()
    store.known_subjects = {UserRef(minor)}
    subjects.subjects[UserRef(minor)] = eligibility_for(False)

    response = client.post("/v1/seeds", json=_seed_body(minor), headers=AUTH)

    assert response.status_code == 201
    assert len(store.seeds) == 1


def test_an_ineligible_subject_gets_403_and_no_run_is_created() -> None:
    """Step-8 done-when. Every assertion here is about absence: no search_runs
    row, no provider client invoked, exactly one audit record.

    A search_runs row with zero results reads as "we looked and found nothing",
    which for a minor is a false reassurance about the exact thing the refusal
    exists to prevent.
    """
    client, store, subjects = make_client()
    user_ref = uuid4()
    subjects.subjects[UserRef(user_ref)] = eligibility_for(False)
    # The seed genuinely EXISTS and is genuinely theirs — a minor can enrol and
    # hold seeds; discovery is what must not run. One seed_id threaded through
    # all three places, so the refusal is proven to come from the eligibility
    # guard and not from an incidental 404 on a seed that was never found.
    seed_id = uuid4()
    store.seeds[seed_id] = SeedRow(
        seed_id=seed_id,
        user_ref=UserRef(user_ref),
        seed_kind="enrolment",
        source_object_ref=SEED_REF,
        status="active",
        created_at=datetime.now(UTC),
    )

    response = client.post(
        "/v1/search",
        json=_search_body(user_ref, seed_id),
        headers=AUTH,
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "discovery_not_available"
    assert store.created_runs == []
    [(refused_ref, outcome, metadata)] = subjects.refusals
    assert refused_ref == user_ref
    assert outcome == "discovery_not_available"
    assert metadata["eligibility_reason"] == "minor_discovery_deferred"
    # The thresholds in force are recorded, so a decision stays interpretable
    # after MIN_DISCOVERY_AGE changes.
    assert metadata["min_discovery_age"] == 18
    assert metadata["min_enrolment_age"] == 13


def test_an_unknown_subject_gets_409_not_403() -> None:
    """Different codes for different situations. 409: the proxy has a user we
    have never enrolled, which it can fix. 403: a minor, which it cannot."""
    client, store, subjects = make_client()
    user_ref = uuid4()

    response = client.post(
        "/v1/search",
        json=_search_body(user_ref, uuid4()),
        headers=AUTH,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "subject_unknown"
    assert store.created_runs == []
    assert [outcome for _, outcome, _ in subjects.refusals] == ["subject_unknown"]


def test_the_eligibility_check_runs_before_the_seed_lookup() -> None:
    """Guard-chain ordering. An ineligible subject with a seed belonging to
    someone else must report the refusal, not the 404: the cheapest and most
    absolute check comes first, and it must not be reachable around."""
    client, store, subjects = make_client()
    owner, minor = uuid4(), uuid4()
    _enrolled_adult(subjects, owner)
    subjects.subjects[UserRef(minor)] = eligibility_for(False)
    seed = client.post("/v1/seeds", json=_seed_body(owner), headers=AUTH).json()

    response = client.post(
        "/v1/search",
        json=_search_body(minor, seed["seed_id"]),
        headers=AUTH,
    )

    assert response.status_code == 403
    assert store.created_runs == []


def test_get_subject_reports_eligibility_and_404s_for_a_stranger() -> None:
    client, _, subjects = make_client()
    adult, minor = uuid4(), uuid4()
    _enrolled_adult(subjects, adult)
    subjects.subjects[UserRef(minor)] = eligibility_for(False)

    assert client.get(f"/v1/subjects/{adult}", headers=AUTH).json() == {
        "discovery_eligible": True,
        "eligibility_reason": "adult",
    }
    assert client.get(f"/v1/subjects/{minor}", headers=AUTH).json() == {
        "discovery_eligible": False,
        "eligibility_reason": "minor_discovery_deferred",
    }
    stranger = client.get(f"/v1/subjects/{uuid4()}", headers=AUTH)
    assert stranger.status_code == 404
    assert stranger.json()["error"]["code"] == "subject_unknown"
    assert client.get(f"/v1/subjects/{adult}").status_code == 401


def test_infringements_nest_attestations_and_serialise_both_score_shapes() -> None:
    """One page found by two providers is ONE entry with TWO attestations,
    and provider_count is the agreement signal the report surface reads."""
    client, store, _subjects = make_client()
    user_ref = uuid4()
    now = datetime.now(UTC)
    store.infringements = [
        InfringementRow(
            infringement_id=uuid4(),
            page_url="https://page/a",
            image_url="https://x/a.jpg",
            keyed_on="page_url",
            first_seen_at=now,
            last_seen_at=now,
            seen_count=2,
            band="review",
            status="new",
            band_reason="unanimous:review(n=2)",
            attestations=(
                AttestationRow(
                    provider_id="hive",
                    score_kind="numeric",
                    provider_score=Decimal("0.8712"),
                    provider_category=None,
                    query_quality="good",
                    score_version="hive-web-search-v1",
                    first_confirmed_at=now,
                    last_confirmed_at=now,
                    confirm_count=3,
                    band="review",
                    calibration_version=None,
                ),
                AttestationRow(
                    provider_id="google",
                    score_kind="categorical",
                    provider_score=None,
                    provider_category="full_match",
                    query_quality=None,
                    score_version="google-web-detection-v1",
                    first_confirmed_at=now,
                    last_confirmed_at=now,
                    confirm_count=1,
                    band="review",
                    calibration_version=None,
                ),
            ),
        )
    ]

    response = client.get(f"/v1/search/infringements?user_ref={user_ref}", headers=AUTH)

    assert response.status_code == 200
    (entry,) = response.json()["infringements"]
    assert entry["page_url"] == "https://page/a"
    assert entry["keyed_on"] == "page_url"
    assert entry["provider_count"] == 2  # two providers, ONE infringement
    assert entry["band"] == "review"  # uncalibrated -> review, no exceptions
    assert entry["seen_count"] == 2

    numeric, categorical = entry["attestations"]
    assert numeric["provider_id"] == "hive"
    assert numeric["score_kind"] == "numeric"
    assert numeric["provider_score"] == 0.8712  # RAW, never rescaled
    assert numeric["provider_category"] is None
    assert numeric["confirm_count"] == 3
    assert categorical["score_kind"] == "categorical"
    assert categorical["provider_score"] is None
    assert categorical["provider_category"] == "full_match"


# --- seed_url: the per-run credential, required on every search -------------


def test_search_without_seed_url_is_400_seed_url_required() -> None:
    """Done-when. No default and no fallback to the seed.

    Falling back to the seed is precisely the bug: the seed holds a durable ref
    now, and before 0011 it held a credential that had already expired. Either
    way, silently substituting it dispatches a search that cannot fetch and
    reports it as a provider failure.
    """
    client, store, subjects = make_client()
    user_ref = uuid4()
    _enrolled_adult(subjects, user_ref)
    seed = client.post("/v1/seeds", json=_seed_body(user_ref), headers=AUTH).json()

    response = client.post(
        "/v1/search",
        json={"user_ref": str(user_ref), "seed_id": seed["seed_id"]},
        headers=AUTH,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "seed_url_required"
    assert store.created_runs == []


@pytest.mark.parametrize(
    "seed_url",
    [
        "http://proxy-s3.example/seed.jpg",
        "s3://bucket/key.jpg",
        "seeds/2026/08/abc.jpg",
        "ftp://proxy-s3.example/seed.jpg",
    ],
)
def test_search_with_a_non_https_seed_url_is_422(seed_url: str) -> None:
    """Done-when. https only — this URL is fetched by a third-party provider
    over the public internet, so plaintext would put the seed image (a photo of
    the user's face) on the wire in the clear."""
    client, store, subjects = make_client()
    user_ref = uuid4()
    _enrolled_adult(subjects, user_ref)
    seed = client.post("/v1/seeds", json=_seed_body(user_ref), headers=AUTH).json()

    response = client.post(
        "/v1/search",
        json=_search_body(user_ref, seed["seed_id"], seed_url=seed_url),
        headers=AUTH,
    )

    assert response.status_code == 422
    assert store.created_runs == []


def test_search_seed_url_is_not_recoverable_from_the_seed() -> None:
    """The two values are independent by construction: a run's URL bears no
    relationship to its seed's ref, and nothing derives one from the other."""
    client, store, subjects = make_client()
    user_ref = uuid4()
    _enrolled_adult(subjects, user_ref)
    seed = client.post("/v1/seeds", json=_seed_body(user_ref), headers=AUTH).json()
    fresh = "https://proxy-s3.example/minted-just-now.jpg?X-Amz-Signature=zzz"

    response = client.post(
        "/v1/search",
        json=_search_body(user_ref, seed["seed_id"], seed_url=fresh),
        headers=AUTH,
    )

    assert response.status_code == 202
    (_, _, _, stored_url) = store.created_runs[-1]
    assert stored_url == fresh
    assert store.seeds[UUID(seed["seed_id"])].source_object_ref == SEED_REF


# --- feedback on a hit ------------------------------------------------------


def _an_infringement(store: FakeSearchStore, owner: UUID) -> UUID:
    infringement_id = uuid4()
    store.infringements.append(
        InfringementRow(
            infringement_id=infringement_id,
            page_url="https://example.test/p",
            image_url="https://example.test/i.jpg",
            keyed_on="page_url",
            first_seen_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
            seen_count=1,
            band="review",
            status="new",
            band_reason=None,
            attestations=(),
        )
    )
    store.owners[infringement_id] = {UserRef(owner)}
    return infringement_id


def _feedback(client: TestClient, infringement_id: UUID, user_ref: UUID, signal: str) -> Any:
    return client.post(
        f"/v1/infringements/{infringement_id}/feedback",
        json={"user_ref": str(user_ref), "signal": signal},
        headers=AUTH,
    )


def test_feedback_404s_identically_for_not_yours_and_not_there() -> None:
    """Done-when: assert both produce byte-identical responses.

    The store returns the same None for both (proven in
    tests/test_infringement_feedback.py against real Postgres); this is the
    other half — that the route does not reintroduce a difference on the way
    out. Everything but request_id, which is per-request by design, must match.
    """
    client, store, _subjects = make_client()
    owner, intruder = uuid4(), uuid4()
    infringement_id = _an_infringement(store, owner)

    not_yours = _feedback(client, infringement_id, intruder, "confirmed")
    not_there = _feedback(client, uuid4(), intruder, "confirmed")

    assert not_yours.status_code == not_there.status_code == 404
    mine, theirs = not_yours.json()["error"], not_there.json()["error"]
    assert {k: v for k, v in mine.items() if k != "request_id"} == {
        k: v for k, v in theirs.items() if k != "request_id"
    }
    assert mine["code"] == "infringement_not_found"
    assert store.feedback == []


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        ("not_me", "dismissed_not_me"),
        ("confirmed", "acknowledged"),
        ("uncertain", "new"),
    ],
)
def test_feedback_returns_the_resulting_status(signal: str, expected: str) -> None:
    client, store, _subjects = make_client()
    owner = uuid4()
    infringement_id = _an_infringement(store, owner)

    response = _feedback(client, infringement_id, owner, signal)

    assert response.status_code == 200
    assert response.json() == {"status": expected}
    assert store.feedback == [(infringement_id, UserRef(owner), signal)]


def test_feedback_rejects_an_unknown_signal_with_422() -> None:
    client, store, _subjects = make_client()
    owner = uuid4()
    infringement_id = _an_infringement(store, owner)

    response = _feedback(client, infringement_id, owner, "definitely_not_me")

    assert response.status_code == 422
    assert store.feedback == []


def test_feedback_rejects_an_unknown_body_field() -> None:
    """extra='forbid'. `suppress_domain` is the field someone will eventually
    try to add, and it is exactly the thing this endpoint must not do."""
    client, store, _subjects = make_client()
    owner = uuid4()
    infringement_id = _an_infringement(store, owner)

    response = client.post(
        f"/v1/infringements/{infringement_id}/feedback",
        json={"user_ref": str(owner), "signal": "not_me", "suppress_domain": True},
        headers=AUTH,
    )

    assert response.status_code == 422
    assert store.feedback == []


def test_feedback_requires_a_service_token() -> None:
    client, _store, _subjects = make_client()
    response = client.post(
        f"/v1/infringements/{uuid4()}/feedback",
        json={"user_ref": str(uuid4()), "signal": "not_me"},
    )
    assert response.status_code == 401
