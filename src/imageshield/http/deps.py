"""Dependency-injection wiring for the FastAPI app.

Long-lived singletons live on ``app.state`` (set by the factory and lifespan)
and reach handlers via ``Depends(get_*)`` — the Flashback pattern
(AgentMeeMaw src/flashback/http/deps.py). Tests override with
``app.dependency_overrides`` or by replacing ``app.state`` attributes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import Request

from imageshield.attribution.provider import FaceAttributionProvider, PhotoFetcher
from imageshield.attribution.store import AttributionStore
from imageshield.config import Config

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from imageshield.enrolment.faceindex import FaceIndex
    from imageshield.enrolment.store import EnrolmentStore
    from imageshield.liveness.provider import LivenessProvider
    from imageshield.liveness.store import LivenessStore
    from imageshield.liveness.uploader import ObjectUploader
    from imageshield.providers.observability import ProviderObservability
    from imageshield.providers.store import ProviderControlStore
    from imageshield.score.store import ScoreStore
    from imageshield.search.store import SearchStore
    from imageshield.subjects.store import SubjectStore
    from imageshield.threats.store import ThreatStore

DbCheck = Callable[[], Awaitable[None]]


def get_config(request: Request) -> Config:
    config: Config = request.app.state.config
    return config


def get_db_pool(request: Request) -> AsyncConnectionPool:
    pool: AsyncConnectionPool = request.app.state.db_pool
    return pool


async def _db_not_ready() -> None:
    raise RuntimeError("database pool is not open")


def get_db_check(request: Request) -> DbCheck:
    check: DbCheck = getattr(request.app.state, "db_check", _db_not_ready)
    return check


def _required_state(request: Request, attribute: str) -> object:
    value = getattr(request.app.state, attribute, None)
    if value is None:
        raise RuntimeError(
            f"app.state.{attribute} is not wired — the lifespan sets it in production;"
            " tests must set it explicitly"
        )
    return value


def get_liveness_store(request: Request) -> LivenessStore:
    store: LivenessStore = _required_state(request, "liveness_store")  # type: ignore[assignment]
    return store


def get_liveness_provider(request: Request) -> LivenessProvider:
    provider: LivenessProvider = _required_state(request, "liveness_provider")  # type: ignore[assignment]
    return provider


def get_object_uploader(request: Request) -> ObjectUploader:
    uploader: ObjectUploader = _required_state(request, "object_uploader")  # type: ignore[assignment]
    return uploader


def get_face_index(request: Request) -> FaceIndex:
    face_index: FaceIndex = _required_state(request, "face_index")  # type: ignore[assignment]
    return face_index


def get_enrolment_store(request: Request) -> EnrolmentStore:
    store: EnrolmentStore = _required_state(request, "enrolment_store")  # type: ignore[assignment]
    return store


def get_search_store(request: Request) -> SearchStore:
    store: SearchStore = _required_state(request, "search_store")  # type: ignore[assignment]
    return store


def get_attribution_store(request: Request) -> AttributionStore:
    store: AttributionStore = _required_state(request, "attribution_store")  # type: ignore[assignment]
    return store


def get_attribution_provider(request: Request) -> FaceAttributionProvider:
    provider: FaceAttributionProvider = _required_state(  # type: ignore[assignment]
        request, "attribution_provider"
    )
    return provider


def get_photo_fetcher(request: Request) -> PhotoFetcher:
    fetcher: PhotoFetcher = _required_state(request, "photo_fetcher")  # type: ignore[assignment]
    return fetcher


def get_subject_store(request: Request) -> SubjectStore:
    store: SubjectStore = _required_state(request, "subject_store")  # type: ignore[assignment]
    return store


def get_provider_control_store(request: Request) -> ProviderControlStore:
    store: ProviderControlStore = _required_state(request, "provider_control_store")  # type: ignore[assignment]
    return store


def get_provider_observability(request: Request) -> ProviderObservability:
    obs: ProviderObservability = _required_state(request, "provider_observability")  # type: ignore[assignment]
    return obs


def get_score_store(request: Request) -> ScoreStore:
    store: ScoreStore = _required_state(request, "score_store")  # type: ignore[assignment]
    return store


def get_threat_store(request: Request) -> ThreatStore:
    store: ThreatStore = _required_state(request, "threat_store")  # type: ignore[assignment]
    return store
