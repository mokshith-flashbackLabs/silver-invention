"""PostgresSearchStore against real Postgres (same convention as
tests/test_enrolment_store.py: own down --all + up arrange step)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from imageshield.confirm.models import ConfirmCriteria
from imageshield.db.connection import make_async_pool
from imageshield.search.cadence import CadenceInput
from imageshield.search.models import ProviderDescriptor
from imageshield.search.provider import ProviderMatch
from imageshield.search.store import PostgresSearchStore, UnknownSubject
from imageshield.types import ProviderId, UserRef
from tests.db import ensure_subject, run_migrate
from tests.providers_fakes import CADENCE

HIVE = ProviderId("hive")
GOOGLE = ProviderId("google")
HIVE_DESC = ProviderDescriptor(
    provider_id=HIVE, score_kind="numeric", score_version="hive-web-search-v1"
)
GOOGLE_DESC = ProviderDescriptor(
    provider_id=GOOGLE, score_kind="categorical", score_version="google-web-detection-v1"
)

# The design-doc §7 defaults (config.py's CONFIRM_HIVE_MIN_SCORE /
# CONFIRM_GOOGLE_KINDS), pinned here rather than read from Config: this
# module tests the store's SQL against fixed criteria, not config wiring.
CONFIRM = ConfirmCriteria(
    hive_min_score=Decimal("0.80"), google_kinds=frozenset({"full_match", "partial_match"})
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


# Deliberately unrelated strings. The seed's ref is durable and opaque; the
# run's URL is a credential the proxy mints per run. Nothing derives one from
# the other, and these tests would not notice if something did unless the two
# values differ.
SEED_REF = "seeds/2026/08/img.jpg"
RUN_SEED_URL = "https://proxy-s3.example/minted.jpg?X-Amz-Signature=fresh"


async def _seeded_run(
    store: PostgresSearchStore,
    user_ref: UserRef,
    *,
    seed_url: str = RUN_SEED_URL,
) -> tuple[UUID, UUID]:
    # Step 8: search_seeds FKs to subjects, so the subject row comes first.
    await ensure_subject(store._pool, user_ref)
    seed_id = await store.create_seed(user_ref, "user_supplied", SEED_REF)
    run_id = await store.create_run(user_ref, seed_id, (HIVE, GOOGLE), seed_url=seed_url)
    return seed_id, run_id


def _query(db_url: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


async def test_seed_roundtrip(store: PostgresSearchStore) -> None:
    user_ref = _user()
    await ensure_subject(store._pool, user_ref)
    seed_id = await store.create_seed(user_ref, "enrolment", "enrolments/ref.jpg")
    seed = await store.get_seed(seed_id)
    assert seed is not None
    assert seed.user_ref == user_ref
    assert seed.seed_kind == "enrolment"
    assert seed.source_object_ref == "enrolments/ref.jpg"
    # Step-8 cadence: a seed created here IS new, and create_seed says so
    # explicitly rather than leaving the column default ("standard") to speak
    # for it — the default is the fallback for a row nobody tiered. Same
    # weekly interval as standard; the tier differs in how it TRANSITIONS.
    assert seed.scan_tier == "new"
    assert seed.consecutive_empty_scans == 0
    assert seed.next_scan_after is None
    assert await store.get_seed(uuid4()) is None


async def test_seed_for_unknown_subject_fails_at_the_database(
    store: PostgresSearchStore,
) -> None:
    """Step-8 done-when: inserting a search_seeds row for an unknown subject
    fails at the DATABASE, not in the application.

    The route guard can be edited or bypassed; this constraint cannot be, which
    is why the eligibility story does not rest on the route alone.

    What escapes is :class:`UnknownSubject` — the route needs a domain error to
    map to 409, because letting psycopg's exception through produced a bare 500
    with none of the error envelope. The `__cause__` assertion is the part that
    matters here: it proves the refusal still originates in the FK and that this
    is a translation, not an application-level pre-check that could drift out of
    agreement with the constraint (or race it).
    """
    with pytest.raises(UnknownSubject) as raised:
        await store.create_seed(_user(), "user_supplied", SEED_REF)

    assert isinstance(raised.value.__cause__, psycopg.errors.ForeignKeyViolation)
    assert "search_seeds_subject_fk" in str(raised.value.__cause__)


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
    assert claim.seed_url == RUN_SEED_URL
    assert claim.providers_attempted == ("hive", "google")

    assert await store.claim_run(run_id) is None  # already claimed, fresh
    assert await store.claim_run(uuid4()) is None  # unknown run


async def test_claim_run_skips_completed(store: PostgresSearchStore) -> None:
    user_ref = _user()
    seed_id, run_id = await _seeded_run(store, user_ref)
    assert await store.claim_run(run_id) is not None
    await store.complete_run(run_id, seed_id, (HIVE,), retier=None, confirm=None)
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


# The provider_calls write moved to PostgresProviderControlStore in step 8, so
# it shares one transaction with the provider_spend upsert and the breaker
# transition. Its tests moved with it, to tests/test_provider_control_store.py —
# including the raw_response-verbatim-on-failure case that lived here.


def _retier(found: bool, *, now: datetime | None = None) -> CadenceInput:
    return CadenceInput(
        found_matches=found, now=now or datetime.now(UTC), policy=CADENCE
    )


async def test_completion_and_cadence_are_one_transaction(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """They were two, and both orderings lost data.

    Completing first makes the run unclaimable, so a crash before the cadence
    write dropped the tier change permanently — SQS would redeliver and claim_run
    would correctly decline a completed run. There is no retry path for a
    half-applied re-tier, so the only fix is that there is no half-applied state.
    """
    user_ref = _user()
    seed_id, run_id = await _seeded_run(store, user_ref)

    update = await store.complete_run(
        run_id, seed_id, (HIVE,), retier=_retier(False), confirm=None
    )

    assert update is not None
    seed = await store.get_seed(seed_id)
    run = await store.get_run(run_id)
    assert seed is not None and run is not None
    # Both halves landed, from one call.
    assert run.status == "completed"
    assert seed.consecutive_empty_scans == 1
    assert seed.next_scan_after is not None
    # A tier without its due date would leave the proxy quoting a cadence the
    # scheduler is not going to honour, so all three columns move as one write.
    assert abs((seed.next_scan_after - update.next_scan_after).total_seconds()) < 1


async def test_a_run_with_no_successful_provider_leaves_the_tier_alone(
    store: PostgresSearchStore,
) -> None:
    """retier=None is how the runner says "this run is not evidence". The run
    still completes; the seed's counter must not move, because demoting a user's
    cadence for our own provider outage takes the saving from the wrong place."""
    user_ref = _user()
    seed_id, run_id = await _seeded_run(store, user_ref)
    before = await store.get_seed(seed_id)
    assert before is not None

    assert (
        await store.complete_run(run_id, seed_id, (), retier=None, confirm=None) is None
    )

    after = await store.get_seed(seed_id)
    run = await store.get_run(run_id)
    assert after is not None and run is not None
    assert run.status == "completed"
    assert after.consecutive_empty_scans == before.consecutive_empty_scans
    assert after.scan_tier == before.scan_tier
    assert after.next_scan_after is None


async def test_concurrent_completions_on_one_seed_lose_no_empty_scan(
    store: PostgresSearchStore,
) -> None:
    """The lost-update race the FOR UPDATE exists for.

    POST /v1/search does not serialise per seed, and _CLAIM_RUN_SQL row-locks
    search_runs, not the joined seed row. Without the lock both runs read the same
    counter and both write count+1, so one empty scan vanishes — at the threshold
    that is the difference between demoting a user to fortnightly and leaving them
    weekly, decided by a race.
    """
    user_ref = _user()
    seed_id, first = await _seeded_run(store, user_ref)
    second = await store.create_run(
        user_ref, seed_id, (HIVE, GOOGLE), seed_url=RUN_SEED_URL
    )

    await asyncio.gather(
        store.complete_run(first, seed_id, (HIVE,), retier=_retier(False), confirm=None),
        store.complete_run(second, seed_id, (HIVE,), retier=_retier(False), confirm=None),
    )

    seed = await store.get_seed(seed_id)
    assert seed is not None
    assert seed.consecutive_empty_scans == 2  # not 1


async def test_a_non_empty_scan_promotes_to_priority_through_the_store(
    store: PostgresSearchStore,
) -> None:
    user_ref = _user()
    seed_id, run_id = await _seeded_run(store, user_ref)

    await store.complete_run(run_id, seed_id, (HIVE,), retier=_retier(True), confirm=None)

    seed = await store.get_seed(seed_id)
    assert seed is not None
    assert seed.scan_tier == "priority"
    assert seed.consecutive_empty_scans == 0


async def test_a_new_seed_starts_on_the_new_tier(
    store: PostgresSearchStore,
) -> None:
    """The 'new' tier has to be written by create_seed. Left to the column
    default ('standard') the whole `new` branch of search/cadence.py was
    unreachable and SCAN_NEW_TIER_WEEKS had no effect at any value, while
    PROXY_INTEGRATION.md advertised a tier the API could never return."""
    user_ref = _user()
    await ensure_subject(store._pool, user_ref)
    seed_id = await store.create_seed(user_ref, "enrolment", "https://s3/img.jpg")

    seed = await store.get_seed(seed_id)
    assert seed is not None
    assert seed.scan_tier == "new"


async def test_run_status_carries_the_seeds_cadence(
    store: PostgresSearchStore,
) -> None:
    """Tiering must never be silent: GET /v1/search/runs reads these off the
    run row, so the join has to bring them through."""
    user_ref = _user()
    seed_id, run_id = await _seeded_run(store, user_ref)
    await store.complete_run(run_id, seed_id, (HIVE,), retier=_retier(True), confirm=None)

    run = await store.get_run(run_id)
    assert run is not None
    assert run.scan_tier == "priority"
    assert run.next_scan_after is not None


async def test_a_refused_run_is_not_completed_and_audits_once(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """A subject who stopped being eligible between enqueue and dispatch.

    The run row already exists, so it cannot be made to disappear; what it must
    not do is read as 'completed', because a completed run with zero results says
    "we looked and found nothing" about a search that never ran (INVARIANTS #8b).
    """
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)

    await store.refuse_run(run_id, user_ref, reason="became ineligible")

    run = await store.get_run(run_id)
    assert run is not None
    assert run.status == "refused"
    assert run.providers_succeeded == ()
    assert run.matches_found == 0

    audit = _query(
        migrated_db,
        "SELECT action, subject_ref, resource_id, metadata FROM audit_log"
        " WHERE resource_id = %s",
        (run_id,),
    )
    assert len(audit) == 1
    assert audit[0][0] == "discovery.refused"
    assert audit[0][1] == user_ref
    assert audit[0][3]["refused_at"] == "dispatch"


async def test_complete_run_sets_status_succeeded_and_count(
    store: PostgresSearchStore,
) -> None:
    user_ref = _user()
    seed_id, run_id = await _seeded_run(store, user_ref)
    await store.record_infringements(
        run_id,
        user_ref,
        HIVE_DESC,
        [_hive_match("https://x/a.jpg"), _hive_match("https://x/b.jpg")],
        {},
    )

    await store.complete_run(run_id, seed_id, (HIVE,), retier=_retier(True), confirm=None)

    run = await store.get_run(run_id)
    assert run is not None
    assert run.status == "completed"
    assert run.providers_attempted == ("hive", "google")
    assert run.providers_succeeded == ("hive",)  # distinguishable from attempted
    assert run.matches_found == 2


async def test_enabled_provider_ids(store: PostgresSearchStore) -> None:
    ids = await store.enabled_provider_ids()
    assert set(ids) == {"hive", "google"}


async def test_enabled_provider_ids_excludes_classifier_rows(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    # rekognition_confirm is seeded enabled=true by 0021 but is not a search
    # provider; attempting it would fabricate a permanent per-run error row.
    ids = await store.enabled_provider_ids()
    assert ProviderId("rekognition_confirm") not in ids


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
        {},
    )
    n_google = await store.record_infringements(
        run_id, user_ref, GOOGLE_DESC, [_google_match(page, "page_match")], {}
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

    await store.record_infringements(run_a, user_a, HIVE_DESC, [match], {})
    await store.record_infringements(run_b, user_b, HIVE_DESC, [match], {})

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
        {},
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
        {},
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
        {},
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
        {},
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
        {},
    )
    _, second_run = await _seeded_run(store, user_ref)
    await store.record_infringements(
        second_run,
        user_ref,
        HIVE_DESC,
        [_hive_match("https://cdn.example/i.jpg", "0.9300", pages=[page])],
        {},
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
        {},
    )
    await store.record_infringements(
        run_id, user_ref, GOOGLE_DESC, [_google_match("https://site/a", "page_match")], {}
    )
    await store.record_infringements(
        other_run,
        other,
        HIVE_DESC,
        [_hive_match("https://x/b.jpg", pages=["https://site/b"])],
        {},
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
    await ensure_subject(store._pool, user_ref)
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
        run_id = await store.create_run(
            user_ref, seed_id, (HIVE, GOOGLE), seed_url=RUN_SEED_URL
        )
        await store.record_infringements(run_id, user_ref, HIVE_DESC, hive_corpus, {})
        await store.record_infringements(run_id, user_ref, GOOGLE_DESC, google_corpus, {})
        await store.complete_run(
            run_id, seed_id, (HIVE, GOOGLE), retier=None, confirm=None
        )
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


async def test_claim_run_seed_url_comes_from_the_run_not_the_seed(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """Done-when: assert with a run whose SEED row holds a deliberately wrong
    value.

    This is the whole point of 0011. If the claim ever reads the seed again, a
    week-old presigned URL comes back, every provider 403s, and it reads as a
    provider outage rather than as our credential expiring. The seed row here
    holds a string that would be obviously wrong to dispatch, so a regression
    cannot pass by coincidence.
    """
    user_ref = _user()
    await ensure_subject(store._pool, user_ref)
    seed_id = await store.create_seed(user_ref, "user_supplied", "DO-NOT-DISPATCH-THIS")
    run_id = await store.create_run(
        user_ref, seed_id, (HIVE,), seed_url="https://proxy-s3.example/right.jpg?sig=1"
    )

    claim = await store.claim_run(run_id)

    assert claim is not None
    assert claim.seed_url == "https://proxy-s3.example/right.jpg?sig=1"
    # ...and the seed is untouched by the run.
    seed = await store.get_seed(seed_id)
    assert seed is not None
    assert seed.source_object_ref == "DO-NOT-DISPATCH-THIS"


async def test_two_runs_on_one_seed_carry_their_own_urls(
    store: PostgresSearchStore,
) -> None:
    """The per-run URL is what makes a re-scan work a month later: the seed is
    unchanged and the proxy mints a fresh credential each time."""
    user_ref = _user()
    await ensure_subject(store._pool, user_ref)
    seed_id = await store.create_seed(user_ref, "user_supplied", SEED_REF)

    first = await store.create_run(
        user_ref, seed_id, (HIVE,), seed_url="https://s3.example/week-1.jpg?sig=a"
    )
    second = await store.create_run(
        user_ref, seed_id, (HIVE,), seed_url="https://s3.example/week-9.jpg?sig=b"
    )

    claim_first = await store.claim_run(first)
    claim_second = await store.claim_run(second)
    assert claim_first is not None and claim_second is not None
    assert claim_first.seed_url == "https://s3.example/week-1.jpg?sig=a"
    assert claim_second.seed_url == "https://s3.example/week-9.jpg?sig=b"


# ── Step 10: confirm-queue enqueue at run completion ──────────────────────


def _infringement_id(migrated_db: str, user_ref: UserRef) -> UUID:
    [row] = _query(
        migrated_db,
        "SELECT infringement_id FROM infringements WHERE user_ref = %s",
        (user_ref,),
    )
    return row[0]  # type: ignore[no-any-return]


async def test_complete_run_enqueues_one_confirm_job_for_a_qualifying_hive_hit(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """Design doc §7: a review-band hit whose hive score clears the 'most
    similar' floor gets a Rekognition triage job — one outbox row, committed
    in the SAME transaction as run completion (INVARIANTS #39's shape)."""
    user_ref = _user()
    seed_id, run_id = await _seeded_run(store, user_ref)
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [_hive_match("https://cdn.example/a.jpg", "0.95")], {}
    )
    infringement_id = _infringement_id(migrated_db, user_ref)

    await store.complete_run(run_id, seed_id, (HIVE,), retier=None, confirm=CONFIRM)

    rows = _query(
        migrated_db,
        "SELECT queue_name, payload FROM outbox WHERE queue_name = 'confirm:hits'",
    )
    assert len(rows) == 1
    assert rows[0][0] == "confirm:hits"
    assert rows[0][1] == {"event": "confirm.hit_requested", "id": str(infringement_id)}


async def test_complete_run_skips_below_floor_hive_and_page_match_google(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """A below-floor hive score and a google `page_match` (not in
    CONFIRM_GOOGLE_KINDS) on the SAME hit qualify neither attestation."""
    user_ref = _user()
    seed_id, run_id = await _seeded_run(store, user_ref)
    page = "https://site.example/below-floor"
    await store.record_infringements(
        run_id,
        user_ref,
        HIVE_DESC,
        [_hive_match("https://cdn.example/a.jpg", "0.50", pages=[page])],
        {},
    )
    await store.record_infringements(
        run_id, user_ref, GOOGLE_DESC, [_google_match(page, "page_match")], {}
    )

    await store.complete_run(
        run_id, seed_id, (HIVE, GOOGLE), retier=None, confirm=CONFIRM
    )

    assert (
        _query(migrated_db, "SELECT 1 FROM outbox WHERE queue_name = 'confirm:hits'")
        == []
    )


async def test_complete_run_does_not_re_enqueue_an_already_triaged_hit(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """confirm_state='unconfirmed' is the re-enqueue guard: a hit already
    machine_triaged has a pending review task, and re-running Rekognition on
    it is pure spend for no new information."""
    user_ref = _user()
    seed_id, run_id = await _seeded_run(store, user_ref)
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [_hive_match("https://cdn.example/a.jpg", "0.95")], {}
    )
    infringement_id = _infringement_id(migrated_db, user_ref)
    _query(
        migrated_db,
        "UPDATE infringements SET confirm_state = 'machine_triaged'"
        " WHERE infringement_id = %s RETURNING infringement_id",
        (infringement_id,),
    )

    await store.complete_run(run_id, seed_id, (HIVE,), retier=None, confirm=CONFIRM)

    assert (
        _query(migrated_db, "SELECT 1 FROM outbox WHERE queue_name = 'confirm:hits'")
        == []
    )


async def test_two_qualifying_attestations_on_one_hit_enqueue_once(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """DISTINCT collapses a hit that clears BOTH providers' bars to one
    outbox row, not two — the same 'one hit, many attestations' rule as
    CLAUDE.md §7.4's dedup, applied to the confirm queue."""
    user_ref = _user()
    seed_id, run_id = await _seeded_run(store, user_ref)
    page = "https://site.example/double-qualified"
    await store.record_infringements(
        run_id,
        user_ref,
        HIVE_DESC,
        [_hive_match("https://cdn.example/a.jpg", "0.95", pages=[page])],
        {},
    )
    await store.record_infringements(
        run_id, user_ref, GOOGLE_DESC, [_google_match(page, "full_match")], {}
    )
    infringement_id = _infringement_id(migrated_db, user_ref)

    await store.complete_run(
        run_id, seed_id, (HIVE, GOOGLE), retier=None, confirm=CONFIRM
    )

    rows = _query(
        migrated_db, "SELECT payload FROM outbox WHERE queue_name = 'confirm:hits'"
    )
    assert len(rows) == 1
    assert rows[0][0] == {"event": "confirm.hit_requested", "id": str(infringement_id)}


async def test_complete_run_with_confirm_none_enqueues_nothing(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """confirm=None is the off switch: verified separately from the
    below-floor case above, this is 'the caller disabled it', not 'nothing
    qualified'."""
    user_ref = _user()
    seed_id, run_id = await _seeded_run(store, user_ref)
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [_hive_match("https://cdn.example/a.jpg", "0.95")], {}
    )

    await store.complete_run(run_id, seed_id, (HIVE,), retier=None, confirm=None)

    assert (
        _query(migrated_db, "SELECT 1 FROM outbox WHERE queue_name = 'confirm:hits'")
        == []
    )
