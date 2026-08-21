"""FastAPI application factory.

Run via uvicorn::

    uvicorn imageshield.http.app:create_app --factory --host 0.0.0.0 --port 8000

or ``python -m imageshield``, which adds the friendlier boot-failure message.

The factory pattern (Flashback convention, AgentMeeMaw src/flashback/http/app.py)
lets tests construct an app with an explicit :class:`Config` and stubbed
``app.state`` without re-importing the module.

The interactive API docs are disabled: this service has no public ingress and
no human callers, and an OpenAPI surface is deployment detail an attacker
shouldn't get for free.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI

from imageshield.attribution.fetch import HttpxPhotoFetcher
from imageshield.attribution.fetch import make_client as make_photo_client
from imageshield.attribution.rekognition import RekognitionFaceAttribution
from imageshield.attribution.store import PostgresAttributionStore
from imageshield.aws_identity import log_aws_identity
from imageshield.config import APP_VERSION, Config, load_config
from imageshield.db.connection import make_async_pool, make_db_check
from imageshield.enrolment.faceindex import RekognitionFaceIndex
from imageshield.enrolment.store import PostgresEnrolmentStore
from imageshield.http.errors import install_error_handlers
from imageshield.http.logging import configure_logging, install_request_logging_middleware
from imageshield.http.routes.admin_providers import router as admin_providers_router
from imageshield.http.routes.admin_review import router as admin_review_router
from imageshield.http.routes.admin_scores import router as admin_scores_router
from imageshield.http.routes.admin_threat_events import router as admin_threat_events_router
from imageshield.http.routes.attribution import router as attribution_router
from imageshield.http.routes.config_floors import router as config_floors_router
from imageshield.http.routes.enrolments import router as enrolments_router
from imageshield.http.routes.health import router as health_router
from imageshield.http.routes.infringements import router as infringements_router
from imageshield.http.routes.liveness import router as liveness_router
from imageshield.http.routes.ping import admin_router, v1_router
from imageshield.http.routes.search import router as search_router
from imageshield.http.routes.subjects import router as subjects_router
from imageshield.liveness.provider import RekognitionLivenessProvider
from imageshield.liveness.store import PostgresLivenessStore
from imageshield.liveness.uploader import HttpxObjectUploader
from imageshield.preview.client import FetcherCropClient
from imageshield.preview.store import PostgresPreviewStore
from imageshield.providers.observability import PostgresProviderObservability
from imageshield.providers.store import PostgresProviderControlStore
from imageshield.review.store import PostgresReviewStore
from imageshield.score.engine import ScoreWeights
from imageshield.score.store import PostgresScoreStore
from imageshield.search.store import PostgresSearchStore
from imageshield.subjects.store import PostgresSubjectStore
from imageshield.threats.store import PostgresThreatStore


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg: Config = app.state.config
    pool = make_async_pool(
        cfg.database_url,
        min_size=cfg.db_pool_min_size,
        max_size=cfg.db_pool_max_size,
    )
    await pool.open()
    app.state.db_pool = pool
    app.state.db_check = make_db_check(pool)
    # The liveness ports. ``getattr`` guards let a caller (tests, the local
    # harness) pre-wire fakes before startup without being overwritten.
    if getattr(app.state, "liveness_store", None) is None:
        app.state.liveness_store = PostgresLivenessStore(pool)
    if getattr(app.state, "liveness_provider", None) is None:
        app.state.liveness_provider = RekognitionLivenessProvider(region=cfg.aws_region)
    if getattr(app.state, "object_uploader", None) is None:
        app.state.object_uploader = HttpxObjectUploader()
    if getattr(app.state, "face_index", None) is None:
        app.state.face_index = RekognitionFaceIndex(region=cfg.aws_region)
    if getattr(app.state, "enrolment_store", None) is None:
        app.state.enrolment_store = PostgresEnrolmentStore(pool)
    if getattr(app.state, "search_store", None) is None:
        app.state.search_store = PostgresSearchStore(pool)
    if getattr(app.state, "subject_store", None) is None:
        app.state.subject_store = PostgresSubjectStore(pool)
    if getattr(app.state, "provider_control_store", None) is None:
        app.state.provider_control_store = PostgresProviderControlStore(
            pool,
            cache_seconds=cfg.provider_config_cache_seconds,
            failure_threshold=cfg.provider_failure_threshold,
            default_cooldown_seconds=cfg.breaker_cooldown_seconds,
            max_cooldown_seconds=cfg.breaker_cooldown_max_seconds,
        )
    if getattr(app.state, "provider_observability", None) is None:
        app.state.provider_observability = PostgresProviderObservability(pool)
    if getattr(app.state, "attribution_store", None) is None:
        app.state.attribution_store = PostgresAttributionStore(pool)
    if getattr(app.state, "attribution_provider", None) is None:
        app.state.attribution_provider = RekognitionFaceAttribution(
            region=cfg.aws_region
        )
    if getattr(app.state, "photo_fetcher", None) is None:
        # Closed in the lifespan's teardown below, with the pool.
        app.state.photo_http_client = make_photo_client()
        app.state.photo_fetcher = HttpxPhotoFetcher(app.state.photo_http_client)
    if getattr(app.state, "score_store", None) is None:
        app.state.score_store = PostgresScoreStore(
            pool,
            weights=ScoreWeights.from_config(cfg),
            config_version=cfg.score_config_version,
        )
    if getattr(app.state, "threat_store", None) is None:
        app.state.threat_store = PostgresThreatStore(pool)
    if getattr(app.state, "review_store", None) is None:
        app.state.review_store = PostgresReviewStore(pool)
    if getattr(app.state, "preview_store", None) is None:
        app.state.preview_store = PostgresPreviewStore(pool)
    if getattr(app.state, "crop_client", None) is None:
        # Closed in the lifespan's teardown below, with the pool.
        app.state.crop_http_client = httpx.AsyncClient(timeout=15.0)
        app.state.crop_client = FetcherCropClient(
            app.state.crop_http_client,
            base_url=cfg.fetcher_base_url,
            token=cfg.fetcher_token,
        )
    log = structlog.get_logger("imageshield.http")
    log.info("service.started", version=APP_VERSION, environment=cfg.environment)
    # Which AWS account and region, before anything touches Rekognition.
    # Someone will eventually run this against production by accident.
    log_aws_identity(region=cfg.aws_region, collection_id=cfg.identity_collection)
    if cfg.auth_disabled:
        log.warning("auth.disabled", environment=cfg.environment)
    elif cfg.service_token_auth_disabled:
        log.error(
            "auth.bypass_ignored",
            reason="SERVICE_TOKEN_AUTH_DISABLED=1 takes effect only when "
            "ENVIRONMENT == 'development'",
            environment=cfg.environment,
        )
    try:
        yield
    finally:
        client = getattr(app.state, "photo_http_client", None)
        if client is not None:
            await client.aclose()
        crop_client = getattr(app.state, "crop_http_client", None)
        if crop_client is not None:
            await crop_client.aclose()
        await pool.close()


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config if config is not None else load_config()
    configure_logging()
    app = FastAPI(
        title="ImageShield Services",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.config = cfg
    install_request_logging_middleware(app)
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(v1_router)
    app.include_router(liveness_router)
    app.include_router(enrolments_router)
    app.include_router(search_router)
    app.include_router(infringements_router)
    app.include_router(attribution_router)
    app.include_router(subjects_router)
    app.include_router(config_floors_router)
    app.include_router(admin_router)
    app.include_router(admin_providers_router)
    app.include_router(admin_threat_events_router)
    app.include_router(admin_review_router)
    app.include_router(admin_scores_router)
    return app
