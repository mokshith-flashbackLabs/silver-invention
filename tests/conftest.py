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

from typing import Any

import pytest
from fastapi.testclient import TestClient

from imageshield.config import Config
from imageshield.http.app import create_app

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
