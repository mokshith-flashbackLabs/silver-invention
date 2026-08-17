"""Shared fixtures.

Tests construct :class:`Config` directly with keyword overrides (the Flashback
convention) — no environment coupling except in the tests that specifically
exercise env loading, which pin their env with monkeypatch and chdir to a tmp
dir so a developer's ``.env.local`` can't leak in.

``TestClient`` is used *without* its context manager so the lifespan (and the
real DB pool it opens) never runs; DB behaviour is stubbed via
``app.state.db_check``.
"""

from __future__ import annotations

import asyncio
import selectors
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from imageshield.calibration.store import PostgresCalibrationStore
from imageshield.config import Config
from imageshield.db.connection import make_async_pool
from imageshield.http.app import create_app
from imageshield.search.store import PostgresSearchStore
from imageshield.types import ProviderId, UserRef
from tests.db import ensure_subject, run_migrate
from tests.db import throwaway_db as throwaway_db  # re-exported for fixture discovery

if sys.platform == "win32":
    # psycopg's async I/O cannot run on Windows' default Proactor event loop
    # (the same constraint src/imageshield/__main__.py already works around
    # for uvicorn — see its comment). Rather than the deprecated
    # `event_loop_policy` fixture-override mechanism, this uses the current
    # pytest-asyncio hook so every async test in the suite (this repo's DB
    # tests, and any future ones e.g. Task 4's relay) gets a selector-based
    # loop with zero deprecation warnings and no per-file duplication.
    def _selector_event_loop() -> asyncio.AbstractEventLoop:
        return asyncio.SelectorEventLoop(selectors.SelectSelector())

    def pytest_asyncio_loop_factories(
        config: pytest.Config, item: pytest.Item
    ) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
        return {"selector": _selector_event_loop}


SERVICE_TOKEN = "service-token-for-tests-0001"
ADMIN_SERVICE_TOKEN = "admin-token-for-tests-0002"

VALID_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql://imageshield:imageshield@localhost:15433/imageshield",
    "SERVICE_TOKEN": SERVICE_TOKEN,
    "ADMIN_SERVICE_TOKEN": ADMIN_SERVICE_TOKEN,
    "AWS_REGION": "ap-south-1",
    "IDENTITY_COLLECTION": "identity-v1",
    "REKOGNITION_REGION": "ap-south-1",
    "DISCOVERED_COLLECTION": "discovered-v1",
    "ENROLMENT_COLLISION_THRESHOLD": "99",
    "SEARCH_MATCH_THRESHOLD": "88.5",
    "ATTRIBUTION_MAX_INFLIGHT": "4",
    "DEV_FACE_CEILING": "50",
    "LIVENESS_MIN_CONFIDENCE": "90",
    "FACE_MATCH_THRESHOLD": "95",
    "MIN_ENROLMENT_AGE": "13",
    "MIN_DISCOVERY_AGE": "18",
    "LIVENESS_SESSION_TTL_SECONDS": "600",
    "LIVENESS_MAX_ATTEMPTS_24H": "5",
    "HIVE_API_KEY": "hive-key-for-tests",
    "HIVE_BASE_URL": "https://api.thehive.ai",
    "GOOGLE_VISION_API_KEY": "google-key-for-tests",
    "SQS_IDENTITY_INDEX_URL": "http://localhost:14566/000000000000/imageshield-identity-index",
    "SQS_SEARCH_RUNS_URL": "http://localhost:14566/000000000000/imageshield-search-runs",
}


def make_config(**overrides: Any) -> Config:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": VALID_ENV["DATABASE_URL"],
        "service_token": SERVICE_TOKEN,
        "admin_service_token": ADMIN_SERVICE_TOKEN,
        "aws_region": "ap-south-1",
        "identity_collection": "identity-v1",
        "rekognition_region": "ap-south-1",
        "discovered_collection": "discovered-v1",
        "enrolment_collision_threshold": 99.0,
        "search_match_threshold": 88.5,
        "attribution_max_inflight": 4,
        "search_provider": "stub",
        "dev_face_ceiling": 50,
        "liveness_min_confidence": 90.0,
        "face_match_threshold": 95.0,
        "min_enrolment_age": 13,
        "min_discovery_age": 18,
        "liveness_session_ttl_seconds": 600,
        "liveness_max_attempts_24h": 5,
        "hive_api_key": "hive-key-for-tests",
        "hive_base_url": "https://api.thehive.ai",
        "google_vision_api_key": "google-key-for-tests",
        "sqs_identity_index_url": VALID_ENV["SQS_IDENTITY_INDEX_URL"],
        "sqs_search_runs_url": VALID_ENV["SQS_SEARCH_RUNS_URL"],
    }
    values.update(overrides)
    return Config(**values)


@pytest.fixture
def config() -> Config:
    return make_config()


@pytest.fixture
def client(config: Config) -> TestClient:
    app = create_app(config=config)
    return TestClient(app)


@pytest.fixture
async def search_fixture(
    throwaway_db: str,
) -> AsyncIterator[tuple[PostgresSearchStore, UUID, UserRef]]:
    """A ``PostgresSearchStore`` against a freshly migrated throwaway
    database, with one seed and one run already created — the arrangement
    ``tests/test_search_store.py`` already does per-test, lifted here so
    ``tests/test_calibration_write_path.py`` doesn't invent a second setup
    path.

    Yields ``(store, run_id, user_ref)``. The run is attempted against both
    known providers, matching ``HIVE_DESC``/``GOOGLE_DESC``'s provider ids.
    """
    down_result = run_migrate(throwaway_db, "down", "--all")
    assert down_result.returncode == 0, down_result.stderr
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr

    pool = make_async_pool(throwaway_db, min_size=1, max_size=2)
    await pool.open()
    try:
        store = PostgresSearchStore(pool)
        user_ref = UserRef(uuid4())
        # Step 8: search_seeds FKs to subjects. Production writes this row inside
        # the enrolment transaction; a fixture that starts from a seed asserts it.
        await ensure_subject(pool, user_ref)
        seed_id = await store.create_seed(user_ref, "user_supplied", "seeds/img.jpg")
        run_id = await store.create_run(
            user_ref,
            seed_id,
            (ProviderId("hive"), ProviderId("google")),
            seed_url="https://proxy-s3.example/run.jpg?X-Amz-Signature=fixture",
        )
        yield store, run_id, user_ref
    finally:
        await pool.close()


@pytest.fixture
async def calibration_store(
    throwaway_db: str,
) -> AsyncIterator[PostgresCalibrationStore]:
    """A ``PostgresCalibrationStore`` over a freshly migrated throwaway
    database — same construction as ``search_fixture`` above, for the eval
    CRUD (``insert_eval_item``, ``eval_rows``, ...) added in Task 5."""
    down_result = run_migrate(throwaway_db, "down", "--all")
    assert down_result.returncode == 0, down_result.stderr
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr

    pool = make_async_pool(throwaway_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresCalibrationStore(pool)
    finally:
        await pool.close()


@pytest.fixture
def google_policy() -> Callable[[str], dict[Any, Any]]:
    """A Google policy mapping every category to one band, for roll-up tests."""
    from imageshield.calibration.models import (
        CalibrationConfig,
        PolicyEntry,
        ScoreDomain,
    )

    def _make(band: str) -> dict[Any, Any]:
        gid = ProviderId("google")
        return {
            gid: PolicyEntry(
                provider_id=gid,
                calibrated=True,
                score_domain=ScoreDomain(
                    categories=("full_match", "partial_match", "page_match")
                ),
                config=CalibrationConfig(
                    config_id=uuid4(),
                    provider_id=gid,
                    version="google-cal-v1",
                    score_kind="categorical",
                    categorical_bands={
                        "full_match": band,
                        "partial_match": band,
                        "page_match": band,
                    },
                ),
            )
        }

    return _make


# ── Task 7 fixtures: eval sets shaped to trip exactly one floor condition ───
#
# Everything in test_calibrate_activate.py depends on eval sets shaped this
# way. One builder plus thin wrappers, so eight hand-written fixtures don't
# drift apart from each other.


async def build_eval_set(
    store: Any,
    *,
    eval_set_id: str = "v1",
    n_true: int = 150,
    n_lookalike: int = 100,
    true_score: str = "0.96",
    lookalike_score: str = "0.60",
    cover: bool = True,
) -> None:
    """One consenting-style eval set with observations, shaped by parameters.

    Defaults are a SOUND set: 250 non-uncertain items, 100 lookalike hard
    negatives, and a clean score separation so a 0.72/0.94 config clears both
    edges. Each unsound fixture below changes exactly one thing, so a failing
    floor test names its own cause.
    """
    hive = ProviderId("hive")
    seed = "s3://seed-a"
    for i in range(n_true):
        item = await store.insert_eval_item(
            eval_set_id, seed, f"https://x.test/t{i}",
            "true_match", "same_person", "team member, written consent", "tester",
        )
        await store.upsert_eval_observation(
            item, hive, "numeric", Decimal(true_score), None, None,
            "hive-web-search-v1",
        )
    for i in range(n_lookalike):
        item = await store.insert_eval_item(
            eval_set_id, seed, f"https://x.test/l{i}",
            "false_match", "lookalike", "team member, written consent", "tester",
        )
        await store.upsert_eval_observation(
            item, hive, "numeric", Decimal(lookalike_score), None, None,
            "hive-web-search-v1",
        )
    if cover:
        await store.record_seed_coverage(
            eval_set_id, seed, hive, "ok", n_true + n_lookalike
        )


HIVE_BANDS_JSON = [
    {"band": "drop", "max": "0.72"},
    {"band": "review", "min": "0.72", "max": "0.94"},
    {"band": "auto_confirm", "min": "0.94"},
]


async def make_calibration_config(store: Any, **kwargs: Any) -> UUID:
    """Insert an inactive Hive config with the standard three bands.

    Named ``make_calibration_config`` rather than ``make_config``: this file
    already has a ``make_config() -> Config`` above, building an entirely
    different thing (the app's boot-time settings object). Same name would
    have silently shadowed it.
    """
    defaults: dict[str, Any] = {
        "provider_id": ProviderId("hive"),
        "version": "hive-cal-v1",
        "score_kind": "numeric",
        "bands": HIVE_BANDS_JSON,
        "eval_set_id": "v1",
        "eval_sample_size": 250,
        "measured": None,
    }
    return await store.insert_config(**{**defaults, **kwargs})


@pytest.fixture
async def sound_eval_set(calibration_store: Any) -> tuple[Any, Any]:
    await build_eval_set(calibration_store)
    return calibration_store, await make_calibration_config(calibration_store)


@pytest.fixture
async def weak_precision_eval_set(calibration_store: Any) -> tuple[Any, Any]:
    """Lookalikes score as high as true matches — nothing separates them."""
    await build_eval_set(calibration_store, lookalike_score="0.96")
    return calibration_store, await make_calibration_config(calibration_store)


@pytest.fixture
async def weak_npv_eval_set(calibration_store: Any) -> tuple[Any, Any]:
    """True matches sit below the drop boundary, so dropping loses them."""
    await build_eval_set(calibration_store, true_score="0.60")
    return calibration_store, await make_calibration_config(calibration_store)


@pytest.fixture
async def small_eval_set(calibration_store: Any) -> tuple[Any, Any]:
    await build_eval_set(calibration_store, n_true=20, n_lookalike=15)
    return calibration_store, await make_calibration_config(
        calibration_store, eval_sample_size=35
    )


@pytest.fixture
async def no_lookalike_eval_set(calibration_store: Any) -> tuple[Any, Any]:
    """250 items, clean separation, precision 1.0 — and meaningless, because
    every negative is an easy one. This is the set the floor must refuse."""
    hive = ProviderId("hive")
    await build_eval_set(calibration_store, n_lookalike=0)
    for i in range(100):
        item = await calibration_store.insert_eval_item(
            "v1", "s3://seed-a", f"https://x.test/u{i}",
            "false_match", "unrelated", "public domain", "tester",
        )
        await calibration_store.upsert_eval_observation(
            item, hive, "numeric", Decimal("0.55"), None, None, "hive-web-search-v1"
        )
    return calibration_store, await make_calibration_config(calibration_store)


@pytest.fixture
async def orphan_config(calibration_store: Any) -> tuple[Any, Any]:
    await build_eval_set(calibration_store)
    return calibration_store, await make_calibration_config(
        calibration_store, eval_set_id=None
    )


@pytest.fixture
async def uncovered_eval_set(calibration_store: Any) -> tuple[Any, Any]:
    await build_eval_set(calibration_store, cover=False)
    return calibration_store, await make_calibration_config(calibration_store)


@pytest.fixture
async def tampered_measured(calibration_store: Any) -> tuple[Any, Any]:
    """measured claims a perfect result; eval_observations disagree. The floor
    must derive from the data, so the claim changes nothing."""
    await build_eval_set(calibration_store, lookalike_score="0.96")
    return calibration_store, await make_calibration_config(
        calibration_store,
        measured={"auto_confirm_precision": 1.0, "drop_npv": 1.0},
    )


@pytest.fixture
async def review_only_config(calibration_store: Any) -> tuple[Any, Any]:
    await build_eval_set(calibration_store, n_true=1, n_lookalike=0)
    return calibration_store, await make_calibration_config(
        calibration_store,
        version="hive-review-only",
        bands=[{"band": "review"}],
    )


@pytest.fixture
async def no_drop_band_config(calibration_store: Any) -> tuple[Any, Any]:
    await build_eval_set(calibration_store)
    return calibration_store, await make_calibration_config(
        calibration_store,
        version="hive-no-drop",
        bands=[
            {"band": "review", "max": "0.94"},
            {"band": "auto_confirm", "min": "0.94"},
        ],
    )


@pytest.fixture
async def two_sound_configs(calibration_store: Any) -> tuple[Any, Any, Any]:
    await build_eval_set(calibration_store)
    first = await make_calibration_config(calibration_store, version="hive-cal-v1")
    second = await make_calibration_config(calibration_store, version="hive-cal-v2")
    return calibration_store, first, second


class BandedFixture:
    """Real infringements with Hive attestations spanning three scores, all
    currently 'review'. Under a 0.72/0.94 config they move in BOTH directions
    (0.60 -> drop, 0.99 -> auto_confirm, 0.80 stays review), so a replay delta
    is non-trivial and by_direction has more than one key."""

    def __init__(self, store: Any, pool: Any) -> None:
        self._store = store
        self._pool = pool

    def entry(self) -> Any:
        from imageshield.calibration.models import (
            CalibrationConfig,
            NumericBand,
            PolicyEntry,
            ScoreDomain,
        )

        hive = ProviderId("hive")
        return PolicyEntry(
            provider_id=hive,
            calibrated=True,
            score_domain=ScoreDomain(min=Decimal("0.5"), max=Decimal("1.0")),
            config=CalibrationConfig(
                config_id=uuid4(),
                provider_id=hive,
                version="hive-cal-v1",
                score_kind="numeric",
                numeric_bands=(
                    NumericBand(band="drop", max=Decimal("0.72")),
                    NumericBand(band="review", min=Decimal("0.72"), max=Decimal("0.94")),
                    NumericBand(band="auto_confirm", min=Decimal("0.94")),
                ),
            ),
        )

    async def counts(self) -> tuple[int, int]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT (SELECT count(*) FROM attestations),"
                "       (SELECT count(*) FROM infringements)"
            )
            row = await cur.fetchone()
        return (row[0], row[1])


@pytest.fixture
async def banded_infringements(calibration_store: Any) -> BandedFixture:
    """Deliberately does NOT depend on the ``search_fixture`` fixture: that
    fixture runs its own ``migrate down --all`` + ``up`` against the same
    session-scoped ``throwaway_db``, and pairing it with ``calibration_store``
    in one test (as ``sound_eval_set`` + this fixture are, throughout the
    activate/trust tests) would re-migrate the schema out from under whatever
    ``sound_eval_set`` already inserted — a config row created, then wiped,
    before the test body ever runs. Building the seed/run directly off
    ``calibration_store``'s own (already-migrated) pool avoids the second
    reset entirely.
    """
    from imageshield.search.models import ProviderDescriptor
    from imageshield.search.provider import ProviderMatch
    from imageshield.search.store import PostgresSearchStore

    store = PostgresSearchStore(calibration_store._pool)
    user_ref = UserRef(uuid4())
    await ensure_subject(calibration_store._pool, user_ref)
    seed_id = await store.create_seed(user_ref, "user_supplied", "seeds/img.jpg")
    run_id = await store.create_run(
        user_ref,
        seed_id,
        (ProviderId("hive"), ProviderId("google")),
        seed_url="https://proxy-s3.example/run.jpg?X-Amz-Signature=fixture",
    )
    hive = ProviderId("hive")
    desc = ProviderDescriptor(
        provider_id=hive, score_kind="numeric", score_version="hive-web-search-v1"
    )
    # Two users, because "how many people does this retune affect" is the
    # number replay exists to report and a single-user fixture cannot prove it.
    second_user = UserRef(uuid4())
    await ensure_subject(calibration_store._pool, second_user)
    for owner, scores in (
        (user_ref, ("0.60", "0.80", "0.99")),
        (second_user, ("0.65", "0.97")),
    ):
        for i, score in enumerate(scores):
            url = f"https://x.test/{owner}/{i}"
            # Empty policy: every row starts at 'review', exactly as the
            # shipped system produces them.
            await store.record_infringements(
                run_id,
                owner,
                desc,
                [
                    ProviderMatch(
                        image_url=f"{url}/img.jpg",
                        page_urls=[url],
                        provider_score=Decimal(score),
                        provider_category=None,
                        query_quality=None,
                    )
                ],
                {},
            )
    return BandedFixture(calibration_store, calibration_store._pool)
