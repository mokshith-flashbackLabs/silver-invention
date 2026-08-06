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

from imageshield.config import Config

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from imageshield.liveness.provider import LivenessProvider
    from imageshield.liveness.store import LivenessStore
    from imageshield.liveness.uploader import ObjectUploader

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
