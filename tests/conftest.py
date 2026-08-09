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
from tests.db import run_migrate
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
    "REKOGNITION_COLLECTION_ID": "identity-v1",
    "LIVENESS_MIN_CONFIDENCE": "90",
    "FACE_MATCH_THRESHOLD": "95",
    "MIN_ENROLMENT_AGE": "18",
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
        "rekognition_collection_id": "identity-v1",
        "liveness_min_confidence": 90.0,
        "face_match_threshold": 95.0,
        "min_enrolment_age": 18,
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
        seed_id = await store.create_seed(user_ref, "user_supplied", "https://s3/img.jpg")
        run_id = await store.create_run(
            user_ref, seed_id, (ProviderId("hive"), ProviderId("google"))
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
