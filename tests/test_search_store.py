"""PostgresSearchStore against real Postgres (same convention as
tests/test_enrolment_store.py: own down --all + up arrange step)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from imageshield.db.connection import make_async_pool
from imageshield.search.models import ProviderDescriptor
from imageshield.search.provider import ProviderMatch, ProviderResult
from imageshield.search.store import PostgresSearchStore
from imageshield.types import ProviderId, UserRef
from tests.db import run_migrate

HIVE = ProviderId("hive")
GOOGLE = ProviderId("google")
HIVE_DESC = ProviderDescriptor(
    provider_id=HIVE, score_kind="numeric", score_version="hive-web-search-v1"
)
GOOGLE_DESC = ProviderDescriptor(
    provider_id=GOOGLE, score_kind="categorical", score_version="google-web-detection-v1"
)


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


@pytest.fixture
async def store(migrated_db: str) -> AsyncIterator[PostgresSearchStore]:
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresSearchStore(pool)
    finally:
        await pool.close()


def _user() -> UserRef:
    return UserRef(uuid4())


def _hive_match(
    url: str, score: str = "0.87", pages: list[str] | None = None
) -> ProviderMatch:
    return ProviderMatch(
        image_url=url,
        page_urls=pages if pages is not None else [f"{url}?page=1"],
        provider_score=Decimal(score),
        provider_category=None,
        query_quality="good",
    )


def _google_match(url: str, category: str = "full_match") -> ProviderMatch:
    return ProviderMatch(
        image_url=url,
        page_urls=[url] if category == "page_match" else [],
        provider_score=None,
        provider_category=category,
        query_quality=None,
    )


async def _seeded_run(
    store: PostgresSearchStore, user_ref: UserRef
) -> tuple[UUID, UUID]:
    seed_id = await store.create_seed(user_ref, "user_supplied", "https://s3/img.jpg")
    run_id = await store.create_run(user_ref, seed_id, (HIVE, GOOGLE))
    return seed_id, run_id


def _query(db_url: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


async def test_seed_roundtrip(store: PostgresSearchStore) -> None:
    user_ref = _user()
    seed_id = await store.create_seed(user_ref, "enrolment", "https://s3/ref.jpg")
    seed = await store.get_seed(seed_id)
    assert seed is not None
    assert seed.user_ref == user_ref
    assert seed.seed_kind == "enrolment"
    assert seed.source_object_uri == "https://s3/ref.jpg"
    assert await store.get_seed(uuid4()) is None


async def test_create_run_writes_outbox_row_in_same_transaction(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)

    rows = _query(
        migrated_db,
        "SELECT queue_name, payload FROM outbox ORDER BY outbox_id DESC LIMIT 1",
    )
    assert rows[0][0] == "search:runs"
    assert rows[0][1] == {"event": "search.run_requested", "id": str(run_id)}

    run = await store.get_run(run_id)
    assert run is not None
    assert run.status == "queued"
    assert run.providers_attempted == ("hive", "google")
    assert run.providers_succeeded == ()
    assert run.matches_found == 0


async def test_claim_run_transitions_queued_to_running_once(
    store: PostgresSearchStore,
) -> None:
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)

    claim = await store.claim_run(run_id)
    assert claim is not None
    assert claim.run_id == run_id
    assert claim.user_ref == user_ref
    assert claim.seed_url == "https://s3/img.jpg"
    assert claim.providers_attempted == ("hive", "google")

    assert await store.claim_run(run_id) is None  # already claimed, fresh
    assert await store.claim_run(uuid4()) is None  # unknown run


async def test_claim_run_skips_completed(store: PostgresSearchStore) -> None:
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)
    assert await store.claim_run(run_id) is not None
    await store.complete_run(run_id, (HIVE,))
    assert await store.claim_run(run_id) is None


async def test_stale_running_claim_is_reclaimable(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)
    assert await store.claim_run(run_id) is not None

    _query(
        migrated_db,
        "UPDATE search_runs SET claimed_at = now() - interval '16 minutes'"
        " WHERE run_id = %s RETURNING run_id",
        (run_id,),
    )
    assert await store.claim_run(run_id) is not None


async def test_record_provider_call_keeps_raw_response_verbatim_on_failure(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)
    result = ProviderResult(
        provider_id=HIVE,
        status="rate_limited",
        matches=[],
        raw_response={"why": "slow down", "nested": {"retry": 60}},
        http_status=429,
        latency_ms=812,
    )

    await store.record_provider_call(run_id, result)

    rows = _query(
        migrated_db,
        "SELECT status, http_status, latency_ms, raw_response FROM provider_calls"
        " WHERE run_id = %s",
        (run_id,),
    )
    assert rows == [("rate_limited", 429, 812, {"why": "slow down", "nested": {"retry": 60}})]


async def test_record_matches_band_review_dedupes_and_null_scores(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)

    inserted = await store.record_matches(
        run_id,
        user_ref,
        HIVE_DESC,
        [_hive_match("https://x/a.jpg"), _hive_match("https://x/a.jpg", "0.91")],
    )
    assert inserted == 1  # same raw URL twice -> one row (run, url, provider)

    inserted = await store.record_matches(
        run_id, user_ref, GOOGLE_DESC, [_google_match("https://x/a.jpg")]
    )
    assert inserted == 1  # same URL from a second provider is an attestation

    rows = _query(
        migrated_db,
        "SELECT provider_id, band, score_kind, provider_score, provider_category,"
        " query_quality, score_version FROM search_matches WHERE run_id = %s"
        " ORDER BY provider_id",
        (run_id,),
    )
    google_row, hive_row = rows
    assert all(row[1] == "review" for row in rows)  # uncalibrated -> review, no exceptions
    assert google_row[0] == "google"
    assert google_row[2:6] == ("categorical", None, "full_match", None)
    assert google_row[6] == "google-web-detection-v1"
    assert hive_row[0] == "hive"
    assert hive_row[2] == "numeric"
    assert hive_row[3] == Decimal("0.87")  # first insert wins; raw, unrescaled
    assert hive_row[4] is None
    assert hive_row[5] == "good"


async def test_complete_run_sets_status_succeeded_and_count(
    store: PostgresSearchStore,
) -> None:
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)
    await store.record_matches(
        run_id,
        user_ref,
        HIVE_DESC,
        [_hive_match("https://x/a.jpg"), _hive_match("https://x/b.jpg")],
    )

    await store.complete_run(run_id, (HIVE,))

    run = await store.get_run(run_id)
    assert run is not None
    assert run.status == "completed"
    assert run.providers_attempted == ("hive", "google")
    assert run.providers_succeeded == ("hive",)  # distinguishable from attempted
    assert run.matches_found == 2


async def test_list_matches_filters_by_user_and_since(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    user_ref, other = _user(), _user()
    _, run_id = await _seeded_run(store, user_ref)
    _, other_run = await _seeded_run(store, other)
    await store.record_matches(run_id, user_ref, HIVE_DESC, [_hive_match("https://x/a.jpg")])
    await store.record_matches(other_run, other, HIVE_DESC, [_hive_match("https://x/b.jpg")])

    mine = await store.list_matches(user_ref, None)
    assert [m.image_url for m in mine] == ["https://x/a.jpg"]
    assert mine[0].score_kind == "numeric"
    assert mine[0].band == "review"

    future = datetime.now(UTC) + timedelta(hours=1)
    assert await store.list_matches(user_ref, future) == ()


async def test_enabled_provider_ids(store: PostgresSearchStore) -> None:
    ids = await store.enabled_provider_ids()
    assert set(ids) == {"hive", "google"}
