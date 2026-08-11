"""Per-domain pacing.

A batch is ordered by whatever is due, and "due" clusters hard by domain: one
site hosting 400 of a user's hits produces 400 consecutive URLs on one host.
Probing those back to back gets the worker blocked, and — more to the point —
looks like an attack from the far end. It is the same traffic shape a scanner
makes.

So the pacer enforces a minimum gap between two requests to the *same* domain,
and imposes nothing at all across different domains: two hosts can be probed
concurrently at full speed, because neither notices the other.

``sleep`` and ``monotonic`` are injected so tests can assert the spacing
without spending it. That is the only reason they are parameters.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class DomainPacer:
    def __init__(
        self,
        min_interval_seconds: float,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._min_interval = min_interval_seconds
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._last_at: dict[str, float] = {}

    async def wait(self, domain: str) -> float:
        """Block until this domain may be probed again. Returns the wait taken.

        The timestamp is recorded as the moment the caller is *released*, not
        the moment it asked — otherwise a queue of waiters on one domain would
        all be cleared to fire at once after a single interval.
        """
        now = self._monotonic()
        previous = self._last_at.get(domain)
        waited = 0.0
        if previous is not None:
            due_at = previous + self._min_interval
            if due_at > now:
                waited = due_at - now
                await self._sleep(waited)
                now = due_at
        self._last_at[domain] = now
        return waited
