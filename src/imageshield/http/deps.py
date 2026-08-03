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
