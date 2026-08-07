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


async def test_complete_run_sets_status_succeeded_and_count(
    store: PostgresSearchStore,
) -> None:
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)
    await store.record_infringements(
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


async def test_enabled_provider_ids(store: PostgresSearchStore) -> None:
    ids = await store.enabled_provider_ids()
    assert set(ids) == {"hive", "google"}


# ── Step 6: dedup, the whole point of the module ──────────────────────────


async def test_cross_provider_is_one_infringement_two_attestations(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """Hive and Google both finding page X for user Y -> ONE infringement
    with TWO attestations, never two infringements. provider_count is a
    genuine agreement signal (CLAUDE.md §7.4)."""
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)
    page = "https://site.example/post/9"

    n_hive = await store.record_infringements(
        run_id,
        user_ref,
        HIVE_DESC,
        [_hive_match("https://cdn.example/img.jpg", pages=[page])],
    )
    n_google = await store.record_infringements(
        run_id, user_ref, GOOGLE_DESC, [_google_match(page, "page_match")]
    )
    assert (n_hive, n_google) == (1, 1)

    infringements = _query(
        migrated_db,
        "SELECT infringement_id, page_url, keyed_on, seen_count FROM infringements"
        " WHERE user_ref = %s",
        (user_ref,),
    )
    assert len(infringements) == 1
    assert infringements[0][1] == page
    assert infringements[0][2] == "page_url"
    assert infringements[0][3] == 2  # two provider observations of one thing

    attestations = _query(
        migrated_db,
        "SELECT provider_id, confirm_count, last_run_id FROM attestations"
        " WHERE infringement_id = %s ORDER BY provider_id",
        (infringements[0][0],),
    )
    assert [(a[0], a[1]) for a in attestations] == [("google", 1), ("hive", 1)]
    assert all(a[2] == run_id for a in attestations)


async def test_cross_user_is_never_dedup(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """The same URL found for two users is TWO infringements. Collapsing
    here is the boundary that leaks one person's matches to another."""
    user_a, user_b = _user(), _user()
    _, run_a = await _seeded_run(store, user_a)
    _, run_b = await _seeded_run(store, user_b)
    match = _hive_match("https://cdn.example/x.jpg", pages=["https://site.example/p"])

    await store.record_infringements(run_a, user_a, HIVE_DESC, [match])
    await store.record_infringements(run_b, user_b, HIVE_DESC, [match])

    rows = _query(
        migrated_db,
        "SELECT user_ref, seen_count FROM infringements WHERE user_ref IN (%s, %s)",
        (user_a, user_b),
    )
    assert len(rows) == 2
    assert {r[0] for r in rows} == {user_a, user_b}
    assert all(r[1] == 1 for r in rows)  # neither user's row counted the other's


async def test_three_backlinks_are_three_infringements(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """One match carrying three backlinks is three places to act."""
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)
    pages = [f"https://site{i}.example/post" for i in range(3)]

    touched = await store.record_infringements(
        run_id,
        user_ref,
        HIVE_DESC,
        [_hive_match("https://cdn.example/img.jpg", pages=pages)],
    )
    assert touched == 3

    rows = _query(
        migrated_db,
        "SELECT page_url, image_url, keyed_on FROM infringements"
        " WHERE user_ref = %s ORDER BY page_url",
        (user_ref,),
    )
    assert [r[0] for r in rows] == sorted(pages)
    assert all(r[1] == "https://cdn.example/img.jpg" for r in rows)
    assert all(r[2] == "page_url" for r in rows)


async def test_same_page_twice_in_one_run_collapses_before_writing(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """The same page returned twice in one run with different image URLs is
    ONE infringement, collapsed BEFORE the write — so seen_count reflects one
    observation, not two. Tracking-param variants collapse too, which is what
    normalisation v1 buys."""
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)

    touched = await store.record_infringements(
        run_id,
        user_ref,
        HIVE_DESC,
        [
            _hive_match("https://cdn.example/a.jpg", pages=["https://site.example/p"]),
            _hive_match(
                "https://cdn.example/b.jpg",
                pages=["https://site.example/p?utm_source=share"],
            ),
        ],
    )
    assert touched == 1

    assert _query(
        migrated_db,
        "SELECT seen_count FROM infringements WHERE user_ref = %s",
        (user_ref,),
    ) == [(1,)]
    assert _query(
        migrated_db,
        "SELECT confirm_count FROM attestations a JOIN infringements i"
        " ON a.infringement_id = i.infringement_id WHERE i.user_ref = %s",
        (user_ref,),
    ) == [(1,)]


async def test_image_url_fallback_when_no_backlink(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """No backlink available -> key on image_url, and record which was used."""
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)

    await store.record_infringements(
        run_id,
        user_ref,
        HIVE_DESC,
        [_hive_match("https://cdn.example/only-image.jpg", pages=[])],
    )
    assert _query(
        migrated_db,
        "SELECT page_url, image_url, keyed_on FROM infringements WHERE user_ref = %s",
        (user_ref,),
    ) == [
        (
            "https://cdn.example/only-image.jpg",
            "https://cdn.example/only-image.jpg",
            "image_url",
        )
    ]


async def test_content_urls_carry_canonical_and_version(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """Debugging a dedup failure without canonical_url means re-deriving the
    normalisation by hand; without the version you cannot tell which rules a
    row was hashed under."""
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)
    await store.record_infringements(
        run_id,
        user_ref,
        HIVE_DESC,
        [
            _hive_match(
                "https://cdn.example/i.jpg",
                pages=["https://Site.example/p/?utm_source=x"],
            )
        ],
    )
    assert _query(
        migrated_db, "SELECT url, canonical_url, normalisation_version FROM content_urls"
    ) == [("https://Site.example/p/?utm_source=x", "https://site.example/p", "v1")]


async def test_rescan_updates_never_inserts_and_moves_the_score(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """Found again -> UPDATE last_seen_at/seen_count and the attestation's
    counters, and take the new provider_score (it may have moved). Never a
    duplicate-key error, never a second row."""
    user_ref = _user()
    _, first_run = await _seeded_run(store, user_ref)
    page = "https://site.example/p"

    await store.record_infringements(
        first_run,
        user_ref,
        HIVE_DESC,
        [_hive_match("https://cdn.example/i.jpg", "0.8100", pages=[page])],
    )
    _, second_run = await _seeded_run(store, user_ref)
    await store.record_infringements(
        second_run,
        user_ref,
        HIVE_DESC,
        [_hive_match("https://cdn.example/i.jpg", "0.9300", pages=[page])],
    )

    rows = _query(
        migrated_db,
        "SELECT i.seen_count, a.confirm_count, a.provider_score, a.last_run_id,"
        " a.first_confirmed_at <= a.last_confirmed_at"
        " FROM infringements i JOIN attestations a"
        " ON a.infringement_id = i.infringement_id WHERE i.user_ref = %s",
        (user_ref,),
    )
    assert len(rows) == 1
    assert rows[0][0] == 2  # seen twice
    assert rows[0][1] == 2  # confirmed twice by hive
    assert rows[0][2] == Decimal("0.9300")  # the newer raw score won
    assert rows[0][3] == second_run  # last_run_id follows the latest run
    assert rows[0][4] is True


async def test_list_infringements_filters_by_user_and_nests_attestations(
    store: PostgresSearchStore,
) -> None:
    user_ref, other = _user(), _user()
    _, run_id = await _seeded_run(store, user_ref)
    _, other_run = await _seeded_run(store, other)
    await store.record_infringements(
        run_id,
        user_ref,
        HIVE_DESC,
        [_hive_match("https://x/a.jpg", pages=["https://site/a"])],
    )
    await store.record_infringements(
        run_id, user_ref, GOOGLE_DESC, [_google_match("https://site/a", "page_match")]
    )
    await store.record_infringements(
        other_run,
        other,
        HIVE_DESC,
        [_hive_match("https://x/b.jpg", pages=["https://site/b"])],
    )

    mine = await store.list_infringements(user_ref, None)
    assert len(mine) == 1  # never another user's row
    assert mine[0].page_url == "https://site/a"
    assert mine[0].keyed_on == "page_url"
    assert mine[0].band == "review"
    assert {a.provider_id for a in mine[0].attestations} == {"hive", "google"}
    assert all(a.confirm_count == 1 for a in mine[0].attestations)
    hive_att = next(a for a in mine[0].attestations if a.provider_id == "hive")
    assert hive_att.provider_score == Decimal("0.8700")  # RAW
    assert hive_att.score_kind == "numeric"

    future = datetime.now(UTC) + timedelta(hours=1)
    assert await store.list_infringements(user_ref, future) == ()
    assert await store.list_infringements(_user(), None) == ()


async def test_52_weekly_rescans_over_static_corpus_add_zero_rows(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """THE regression test for the defect step 6 exists to fix.

    The old system's matches[].seenInScans appended one entry per scan
    forever (weeklyInfringementScanner.js:1016). Row count here must grow
    with CONTENT, never with TIME: 52 weekly rescans of a static corpus add
    zero rows, and only seen_count, confirm_count, last_seen_at and
    last_confirmed_at move.

    DO NOT DELETE OR WEAKEN THIS TEST — step-6 spec, "Done when".
    """
    user_ref = _user()
    seed_id = await store.create_seed(user_ref, "user_supplied", "https://s3/img.jpg")
    # Three pages, each found by Hive (via a backlink) and by Google (as a
    # page match) — the cross-provider case, rescanned.
    hive_corpus = [
        _hive_match(f"https://cdn.example/{i}.jpg", pages=[f"https://site{i}.example/p"])
        for i in range(3)
    ]
    google_corpus = [
        _google_match(f"https://site{i}.example/p", "page_match") for i in range(3)
    ]

    def _counts() -> tuple[int, int, int]:
        return (
            _query(migrated_db, "SELECT count(*) FROM infringements")[0][0],
            _query(migrated_db, "SELECT count(*) FROM attestations")[0][0],
            _query(migrated_db, "SELECT count(*) FROM content_urls")[0][0],
        )

    first_week_counts: tuple[int, int, int] | None = None
    for week in range(52):
        run_id = await store.create_run(user_ref, seed_id, (HIVE, GOOGLE))
        await store.record_infringements(run_id, user_ref, HIVE_DESC, hive_corpus)
        await store.record_infringements(run_id, user_ref, GOOGLE_DESC, google_corpus)
        await store.complete_run(run_id, (HIVE, GOOGLE))
        if week == 0:
            first_week_counts = _counts()

    # 3 pages -> 3 infringements, 2 attestations each, 3 content_urls
    assert first_week_counts == (3, 6, 3)
    assert _counts() == first_week_counts  # 51 further rescans added ZERO rows

    rows = _query(
        migrated_db,
        "SELECT seen_count FROM infringements WHERE user_ref = %s",
        (user_ref,),
    )
    assert all(r[0] == 104 for r in rows)  # 2 providers x 52 observations
    assert all(
        r[0] == 52 for r in _query(migrated_db, "SELECT confirm_count FROM attestations")
    )

    # The read surface still shows three things to act on, not 312.
    listed = await store.list_infringements(user_ref, None)
    assert len(listed) == 3
    assert all(len(inf.attestations) == 2 for inf in listed)
