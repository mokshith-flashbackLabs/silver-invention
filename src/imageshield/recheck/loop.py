"""One pass of the recheck loop: take a batch, probe it, record the verdicts.

Separated from ``worker.py`` (the process, the signals, the sleep) so the
behaviour can be tested without a scheduler around it — the same split as
``search/runner.py`` versus ``search/worker.py``.

The allowlist is read **once per batch**, not once per URL. It is a
`SELECT DISTINCT` over `content_urls` and it does not change mid-pass in any
way that matters; per-URL would be a query per probe for the same answer.
"""

from __future__ import annotations

import structlog

from imageshield.recheck.client import UrlChecker
from imageshield.recheck.models import CheckResult
from imageshield.recheck.pacer import DomainPacer
from imageshield.recheck.store import RecheckStore

log = structlog.get_logger("imageshield.recheck")


async def run_once(
    store: RecheckStore,
    checker: UrlChecker,
    pacer: DomainPacer,
    *,
    interval_days: int,
    batch_size: int,
) -> tuple[CheckResult, ...]:
    """Probe one batch. Returns every result, including the refusals."""
    batch = await store.due_batch(interval_days=interval_days, limit=batch_size)
    if not batch:
        return ()
    allowed = await store.allowed_domains()

    results: list[CheckResult] = []
    for item in batch:
        # Sequential, and deliberately. The pacer's whole job is to space
        # same-domain requests, and a due batch clusters hard by domain — one
        # site hosting 400 of a user's hits produces 400 consecutive URLs on one
        # host. Fanning out concurrently would defeat it for exactly the case it
        # exists for. This loop is weekly and unhurried; it can afford to walk.
        await pacer.wait(item.source_domain)
        result = await checker.check(item, allowed)
        await store.record_verdict(result.infringement_id, result.verdict)
        results.append(result)

    dead = sum(1 for r in results if r.verdict == "dead")
    refused = sum(1 for r in results if r.refused is not None)
    log.info(
        "recheck.batch_completed",
        checked=len(results),
        dead=dead,
        refused=refused,
        unchanged=sum(1 for r in results if r.verdict == "unchanged"),
    )
    return tuple(results)
