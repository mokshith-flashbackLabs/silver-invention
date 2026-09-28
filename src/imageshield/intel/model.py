"""The one place a language model is called (CLAUDE.md §2 amended 2026-09-28).

Claude Platform on AWS via AnthropicAWS: SigV4 with the task role, no API key.
``max_retries=0`` on the client -- our own bounded retry keeps a future
``provider_calls.attempt`` honest; the SDK's hidden retries would under-report
it. Server-side fallbacks are OFF: a declined document is recorded, never
rescued by another model (#4 for intel).

**Why this calls ``messages.create`` and never ``messages.parse``.** In
``anthropic==1.8.0``, ``.parse()`` validates the response's text block with
``TypeAdapter.validate_json`` INSIDE the call and raises ``pydantic.ValidationError``
straight out of it on bad/truncated/non-JSON output -- before returning anything,
including ``usage``. A billed call that fails to parse would then vanish with no
usage and no cost recorded, and the caller (task 9's ``metered`` wrapper) only
catches ``ModelUnavailable``. So every call here goes through ``messages.create``
with the same structured-output ``output_config`` ``.parse()`` builds internally
(``_output_config`` below, verified against ``anthropic.lib._parse._transform
.transform_schema`` and ``anthropic.resources.messages.Messages.parse``'s own
source in the installed package -- there is no public helper that builds this
from a pydantic model without going through ``.parse()`` itself), then classifies
the raw ``Message`` by hand: ``stop_reason`` first (``refusal``/``max_tokens`` are
neutral, no parse attempted), only then does a caught ``model_validate_json`` on
the final text block. Usage, cost and latency are attached on every one of those
paths -- only a genuine transport/status failure (which never returns a
``Message`` at all) raises, for the metering layer to classify as unavailable.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Generic, Literal, Protocol, TypeVar

import anthropic
from anthropic.lib._parse._transform import transform_schema
from pydantic import BaseModel, TypeAdapter, ValidationError

from imageshield.intel.config import IntelConfig
from imageshield.intel.pricing import Usage, cost_of
from imageshield.intel.schemas import DiscoveryOutput, ExtractionOutput

T = TypeVar("T", bound=BaseModel)
Outcome = Literal["ok", "refusal", "max_tokens", "unparseable"]
_RETRIES = 3
_MAX_TOKENS = 8000


@dataclass(frozen=True)
class ModelCall(Generic[T]):
    output: T | None
    outcome: Outcome
    answered_by: str
    stop_reason: str
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    pause_turns: int = 0
    # The raw response content blocks, kept only so a pause_turn can be
    # resumed (discover()'s loop). Never persisted and never put in a stored
    # raw_response column (spec §5) -- it is a transport detail, not evidence.
    content: list[Any] = field(default_factory=list)


class ModelUnavailable(Exception):
    """A call never came back as a ``Message`` at all -- timeout, a 5xx/429
    status, or a connection failure. Distinct from every ``ModelCall`` outcome,
    which all describe an answer the model DID return."""

    def __init__(self, status: Literal["timeout", "error", "rate_limited"], detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class IntelModel(Protocol):
    async def extract(self, system: str, user: str) -> ModelCall[ExtractionOutput]: ...
    async def discover(self, system: str, user: str) -> ModelCall[DiscoveryOutput]: ...


def _usage(raw: Any) -> Usage:
    tool = getattr(raw, "server_tool_use", None)
    return Usage(
        input_tokens=raw.input_tokens or 0,
        output_tokens=raw.output_tokens or 0,
        cache_creation_input_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        web_search_requests=(getattr(tool, "web_search_requests", 0) or 0) if tool else 0,
    )


def _output_config(output_format: type[BaseModel]) -> dict[str, Any]:
    """Build ``output_config`` exactly the way ``Messages.parse`` builds it
    internally (same ``TypeAdapter(...).json_schema()`` +
    ``transform_schema(...)`` pair, read from the installed
    ``anthropic==1.8.0`` source) -- so a hand-rolled ``.create()`` call puts
    the same structured-output request on the wire ``.parse()`` would have.
    """
    schema = TypeAdapter(output_format).json_schema()
    return {"format": {"type": "json_schema", "schema": transform_schema(schema)}}


class ClaudeIntelModel:
    def __init__(self, config: IntelConfig, *, client: Any | None = None) -> None:
        # Priced BEFORE any call, not after: a call is billed the moment the API
        # answers (see `_send` below), so an `UnknownModelPrice` escaping AFTER a
        # billed call would leave that call unrecorded while the run retries it
        # -- up to MAX_RUN_ATTEMPTS times, each one billed and unmetered. Pricing
        # the configured (requested) model id here, once, means `_send` and
        # `discover` can never raise pricing a call that already happened: the
        # id this process sends never changes mid-run.
        cost_of(config.intel_extraction_model, Usage(0, 0, 0, 0, 0))
        self._config = config
        # Explicitly `Any`: an injected test double (SimpleNamespace) is not an
        # AsyncAnthropicAWS, and `self._client.messages.create(**kwargs)` below
        # must not be checked against the real client's narrow model Literal.
        self._client: Any = client or anthropic.AsyncAnthropicAWS(
            aws_region=config.intel_anthropic_region,
            workspace_id=config.anthropic_aws_workspace_id,
            max_retries=0,
        )

    async def _send(self, output_format: type[T], **kwargs: Any) -> ModelCall[T]:
        started = time.monotonic()
        response: Any = None
        for attempt in range(1, _RETRIES + 1):
            try:
                response = await self._client.messages.create(
                    model=self._config.intel_extraction_model,
                    max_tokens=_MAX_TOKENS,
                    thinking={"type": "adaptive"},
                    output_config=_output_config(output_format),
                    **kwargs,
                )
                break
            except anthropic.RateLimitError as exc:
                if attempt == _RETRIES:
                    raise ModelUnavailable("rate_limited", type(exc).__name__) from exc
            except anthropic.APITimeoutError as exc:
                raise ModelUnavailable("timeout", type(exc).__name__) from exc
            except anthropic.APIStatusError as exc:
                if exc.status_code < 500 or attempt == _RETRIES:
                    raise ModelUnavailable(
                        "error", f"{type(exc).__name__}:{exc.status_code}"
                    ) from exc
            except anthropic.APIConnectionError as exc:
                if attempt == _RETRIES:
                    raise ModelUnavailable("error", type(exc).__name__) from exc
            await asyncio.sleep(min(8.0, 0.5 * 2**attempt) * (0.5 + random.random()))
        assert response is not None

        usage = _usage(response.usage)
        stop = str(response.stop_reason)
        content = list(getattr(response, "content", []) or [])
        output: T | None = None
        outcome: Outcome

        if stop == "refusal":
            outcome = "refusal"
        elif stop == "max_tokens":
            outcome = "max_tokens"
        else:
            # The FINAL text block: a thinking/tool-use block may precede it,
            # and parse_response (the SDK's own .parse() helper) attaches
            # parsed output to every text block it finds, last one standing.
            text: str | None = None
            for block in content:
                if getattr(block, "type", None) == "text":
                    text = block.text
            if text is None:
                outcome = "unparseable"
            else:
                try:
                    output = output_format.model_validate_json(text)
                    outcome = "ok"
                except ValidationError:
                    outcome = "unparseable"

        return ModelCall(
            output=output,
            outcome=outcome,
            # The model that ANSWERED, recorded for logging/debugging -- may
            # differ from what was requested (a routing fallback) and is never
            # what prices the call (see `__init__`'s pre-check).
            answered_by=str(response.model),
            stop_reason=stop,
            usage=usage,
            cost_usd=cost_of(self._config.intel_extraction_model, usage),
            latency_ms=int((time.monotonic() - started) * 1000),
            content=content,
        )

    async def extract(self, system: str, user: str) -> ModelCall[ExtractionOutput]:
        return await self._send(
            ExtractionOutput, system=system, messages=[{"role": "user", "content": user}]
        )

    async def discover(self, system: str, user: str) -> ModelCall[DiscoveryOutput]:
        tools: list[dict[str, Any]] = [
            {
                "type": self._config.intel_web_search_tool_type,
                "name": "web_search",
                "max_uses": self._config.intel_max_web_searches_per_run,
                **(
                    {"blocked_domains": self._config.intel_blocked_domains}
                    if self._config.intel_blocked_domains
                    else {}
                ),
            }
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        pause_turns = 0
        total_calls = 0  # every actual API call this discover() makes, initial included
        total = Usage(0, 0, 0, 0, 0)
        while True:
            call = await self._send(DiscoveryOutput, system=system, tools=tools, messages=messages)
            total_calls += 1
            u = call.usage
            total = Usage(
                total.input_tokens + u.input_tokens,
                total.output_tokens + u.output_tokens,
                total.cache_creation_input_tokens + u.cache_creation_input_tokens,
                total.cache_read_input_tokens + u.cache_read_input_tokens,
                total.web_search_requests + u.web_search_requests,
            )
            # `total_calls`, not `pause_turns`: the cap bounds every call this
            # method makes, not merely the resumes after the first. Checked
            # before resuming again -- the off-by-one this replaces let one
            # discover() make `max_calls_per_run + 1` calls (the initial call
            # was never counted against its own cap).
            if (
                call.stop_reason != "pause_turn"
                or total_calls >= self._config.intel_max_calls_per_run
            ):
                return ModelCall(
                    output=call.output,
                    outcome=call.outcome,
                    answered_by=call.answered_by,
                    stop_reason=call.stop_reason,
                    usage=total,
                    cost_usd=cost_of(self._config.intel_extraction_model, total),
                    latency_ms=call.latency_ms,
                    pause_turns=pause_turns,
                )
            pause_turns += 1
            # Resume per Anthropic's stop-reason handling guidance: resend the
            # original user turn plus the paused assistant turn verbatim; the
            # server resumes from the trailing server_tool_use block.
            messages = [messages[0], {"role": "assistant", "content": call.content}]
