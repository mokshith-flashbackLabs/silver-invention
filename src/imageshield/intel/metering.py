"""Every model call goes through the provider gate (spec §5, INVARIANTS #37-41).

For kind llm a NULL daily budget REFUSES (budget_unset) -- the inverse of search
providers, deliberately: a model that can loop on web search must not run uncapped.
raw_response holds metadata only, never content, quotes or search results (spec §5).
A refusal, max_tokens or unparseable output is status 'ok': the call worked, the
verdict lives in intel tables, and #40 forbids opening a breaker on an ordinary result.

Each call also ends one step of the run log (spec 2026-10-03 §3.5), when the worker has bound
one: a refusal before the call is ``model_call_skipped``, an unavailable model
``model_call_failed`` (with the API's own message, which ``provider_calls.error_detail`` never
carries), an answer ``model_call_finished``. Written AFTER the metering row, and never able to
fail the call.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel

from imageshield.intel.model import ModelCall, ModelUnavailable
from imageshield.intel.run_log import note_failed, note_finished, note_skipped
from imageshield.providers.gate import decide
from imageshield.providers.models import Skip
from imageshield.providers.store import ProviderControlStore, utc_spend_date
from imageshield.search.provider import ProviderResult
from imageshield.types import ProviderId

T = TypeVar("T", bound=BaseModel)
CLAUDE_INTEL = ProviderId("claude_intel")
MeteredOutcome = Literal["called", "skipped", "budget_unset"]


async def metered(
    control: ProviderControlStore,
    *,
    run_id: UUID,
    now: datetime,
    call: Callable[[], Awaitable[ModelCall[T]]],
) -> tuple[MeteredOutcome, ModelCall[T] | None, str | None]:
    """Run one model call through the same guard chain search providers use.

    ``run_id`` here is an ``intel_runs.run_id`` -- it lands on
    ``provider_calls.intel_run_id``, never on ``provider_calls.run_id`` (that
    column names a ``search_runs`` row, and ``provider_calls_one_run`` forbids
    both being non-NULL on the same row).

    A NULL ``daily_budget_usd`` refuses before ``decide`` ever runs (and before
    ``call`` is invoked at all): the inverse of a search provider, where an
    unset budget is a valid, if unusual, "no cap" reading. A model that can
    loop on web search tool calls must not be allowed to run uncapped by a row
    an operator never got around to filling in -- invariant #38's fail-closed
    reasoning, applied to the one provider kind where "uncapped" is a live
    risk rather than a historical accident.

    Every outcome the model itself can produce -- ``ok``, ``refusal``,
    ``max_tokens``, ``unparseable`` -- is recorded as ``provider_calls.status
    = 'ok'``: the call worked, a ``Message`` came back, and the verdict on
    *what it said* lives in the intel tables, not in the breaker. Only a
    genuine transport failure (:class:`ModelUnavailable` -- timeout, a 5xx/429
    status, a connection failure) is recorded under its own status and can
    move the breaker.
    """
    runtimes = await control.runtimes()
    runtime = runtimes.get(CLAUDE_INTEL)
    if runtime is not None and runtime.enabled and runtime.daily_budget_usd is None:
        await note_skipped("budget_unset")
        return "budget_unset", None, "budget_unset"

    decision = await decide(CLAUDE_INTEL, runtime=runtime, store=control, now=now)
    if isinstance(decision, Skip):
        await control.record_skip(
            None, CLAUDE_INTEL, decision.reason, decision.detail, intel_run_id=run_id
        )
        await note_skipped(decision.reason)
        return "skipped", None, decision.reason

    started = time.monotonic()
    try:
        result = await call()
    except ModelUnavailable as exc:
        await control.record_outcome(
            None,
            ProviderResult(
                provider_id=CLAUDE_INTEL,
                status=exc.status,
                matches=[],
                raw_response={"outcome": exc.status},
                http_status=None,
                latency_ms=0,
                error_detail=exc.detail,
            ),
            cost_usd=decision.cost_usd,
            spend_date=utc_spend_date(now),
            probe=decision.probe,
            intel_run_id=run_id,
        )
        await note_failed(exc)
        return "called", None, exc.status

    await control.record_outcome(
        None,
        ProviderResult(
            provider_id=CLAUDE_INTEL,
            status="ok",
            matches=[],
            raw_response={
                "model": result.answered_by,
                "stop_reason": result.stop_reason,
                "usage": {
                    "input": result.usage.input_tokens,
                    "output": result.usage.output_tokens,
                    "cache_write": result.usage.cache_creation_input_tokens,
                    "cache_read": result.usage.cache_read_input_tokens,
                },
                "web_search_requests": result.usage.web_search_requests,
                "pause_turns": result.pause_turns,
                "outcome": result.outcome,
            },
            http_status=None,
            latency_ms=result.latency_ms,
        ),
        cost_usd=result.cost_usd,
        spend_date=utc_spend_date(now),
        probe=decision.probe,
        intel_run_id=run_id,
    )
    await note_finished(result, int((time.monotonic() - started) * 1000))
    return "called", result, None
