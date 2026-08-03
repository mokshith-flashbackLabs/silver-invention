"""Postgres connection pool factory.

Adapted from the Flashback agent service (AgentMeeMaw
src/flashback/db/connection.py) minus the pgvector registration — this repo
holds no vectors until we own embeddings (CLAUDE.md §2).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from psycopg_pool import AsyncConnectionPool


def make_async_pool(
    database_url: str,
    *,
    min_size: int = 1,
    max_size: int = 4,
    max_lifetime: float = 1800,
    max_idle: float = 600,
) -> AsyncConnectionPool:
    """Build an async psycopg pool for the HTTP service.

    Constructed *unopened* — FastAPI's lifespan calls ``await pool.open()``
    and ``await pool.close()``; constructing with ``open=True`` would require
    a running event loop the factory shouldn't assume.
    """
    return AsyncConnectionPool(
        conninfo=database_url,
        min_size=min_size,
        max_size=max_size,
        max_lifetime=max_lifetime,
        max_idle=max_idle,
        open=False,
    )


def make_db_check(
    pool: AsyncConnectionPool, *, timeout_seconds: float = 2.0
) -> Callable[[], Awaitable[None]]:
    """Health-check probe: one trivial query behind a hard deadline, so a
    wedged database turns into 'degraded' instead of a hanging /health."""

    async def _check() -> None:
        async def _query() -> None:
            async with pool.connection(timeout=timeout_seconds) as conn:
                await conn.execute("SELECT 1")

        await asyncio.wait_for(_query(), timeout=timeout_seconds)

    return _check
