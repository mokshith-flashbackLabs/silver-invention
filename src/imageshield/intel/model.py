"""The one place a language model is called (CLAUDE.md §2 amended 2026-09-28).

Claude Platform on AWS via AnthropicAWS: SigV4 with the task role, no API key.
``max_retries=0`` on the client -- our own bounded retry keeps a future
``provider_calls.attempt`` honest; the SDK's hidden retries would under-report
it. Server-side fallbacks are OFF: a declined document is recorded, never
rescued by another model (#4 for intel).

**Why this never calls ``messages.parse``.** In
``anthropic==1.8.0``, ``.parse()`` validates the response's text block with
``TypeAdapter.validate_json`` INSIDE the call and raises ``pydantic.ValidationError``
straight out of it on bad/truncated/non-JSON output -- before returning anything,
including ``usage``. A billed call that fails to parse would then vanish with no
usage and no cost recorded, and the caller (task 9's ``metered`` wrapper) only
catches ``ModelUnavailable``. So every call here went through ``messages.create``
(``messages.stream`` since 2026-10-03, below) with the same structured-output
``output_config`` ``.parse()`` builds internally
(``_output_config`` below, verified against ``anthropic.lib._parse._transform
.transform_schema`` and ``anthropic.resources.messages.Messages.parse``'s own
source in the installed package -- there is no public helper that builds this
from a pydantic model without going through ``.parse()`` itself), then classifies
the raw ``Message`` by hand: ``stop_reason`` first (``refusal``/``max_tokens`` are
neutral, no parse attempted), only then does a caught ``model_validate_json`` on
the final text block. Usage, cost and latency are attached on every one of those
paths -- only a genuine transport/status failure (which never returns a
``Message`` at all) raises, for the metering layer to classify as unavailable.

**Every call is STREAMED (the run log, spec 2026-10-03 §3.4).**
``messages.stream`` takes the same arguments ``create`` took, and the request it
sends is the same apart from ``stream: true``. Passing ``output_config`` and never
``output_format`` keeps the stream from parsing anything itself either
(``AsyncMessageStreamManager``'s ``output_format`` stays omitted, so
``accumulate_event`` never calls ``parse_text`` at a ``content_block_stop``), and
``get_final_message()`` hands back the accumulated ``Message`` this module has
always classified: same ``ModelCall``, same usage, same pricing, same outcome.
The events are fed to ``run_log.StreamTranslator`` on the way through, so an
operator can watch a call of several minutes as it happens. Thinking is
``{"type": "adaptive", "display": "summarized"}``: a readable summary for that
log (``display`` does not change billing).

Failures are classified as before, now raised from entering OR iterating the
stream, plus two shapes only a stream has: an ``error`` event inside an open
stream (an ``APIStatusError`` carrying the stream's own 200, a server-side
failure such as ``overloaded_error``, retried like a 5xx), and a transport error
mid-body, which the SDK does not wrap (``httpx2``'s, mapped exactly as the SDK
maps the same error before a body: a timeout is ``timeout``, anything else a
retried connection failure).
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Generic, Literal, Protocol, TypeVar

import anthropic
import httpx2
from anthropic.lib._parse._transform import transform_schema
from pydantic import BaseModel, TypeAdapter, ValidationError

from imageshield.intel.config import IntelConfig
from imageshield.intel.pricing import Usage, cost_of
from imageshield.intel.run_log import (
    MAX_FAILURE_MESSAGE_CHARS,
    StreamTranslator,
    current_run_log,
    note_continuing,
    note_model_call_started,
)
from imageshield.intel.schemas import (
    DiscoveryOutput,
    ExtractionOutput,
    ProposalOutput,
    SourceProposalOutput,
    SuggestionOutput,
)

T = TypeVar("T", bound=BaseModel)
Outcome = Literal["ok", "refusal", "max_tokens", "unparseable"]
_RETRIES = 3
_MAX_TOKENS = 8000
# Opus 5.5 defaults to effort "medium" (spec §4.3 says set it explicitly). max_tokens stays
# _MAX_TOKENS: the 0.45 worst case in migration 0041 assumes 8 000 output tokens, and a
# proposal call that stops there is consumed and counted (proposal_model_max_tokens).
_PROPOSAL_EFFORT = "high"
# spec §4.10 stage 3: a search_query candidate costs ONE web search.
_VALIDATION_SEARCHES = 1
# The run log shows Claude's reasoning summary (spec 2026-10-03 §2): on Claude 5 models the
# default display is "omitted", an empty text. Billing does not depend on `display`.
_THINKING = {"type": "adaptive", "display": "summarized"}


@dataclass(frozen=True)
class WebResult:
    """One result a web search returned: its URL and the search's own ``page_age`` for it (a
    date or a relative age such as "3 days ago", or None). The page's publication date when
    nothing better is known (spec 2026-10-04-intel-evidence-quality §2); never evidence."""

    url: str
    page_age: str | None


def _field(obj: object, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def web_results(content: list[Any]) -> list[WebResult]:
    """Every ``web_search_result`` in a response's ``web_search_tool_result`` blocks. A block
    whose content is an error object rather than a list carries none."""
    found: list[WebResult] = []
    for block in content:
        if _field(block, "type") != "web_search_tool_result":
            continue
        results = _field(block, "content")
        if not isinstance(results, list):
            continue
        for result in results:
            url = _field(result, "url")
            if _field(result, "type") != "web_search_result" or not isinstance(url, str):
                continue
            age = _field(result, "page_age")
            found.append(WebResult(url, age if isinstance(age, str) and age.strip() else None))
    return found


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
    # Every result a web search returned across all its continuations, with its page_age
    # (spec 2026-10-04-intel-evidence-quality §2). Empty for a call without the search tool.
    search_results: tuple[WebResult, ...] = ()


class ModelUnavailable(Exception):
    """A call never came back as a ``Message`` at all -- timeout, a 5xx/429
    status, or a connection failure. Distinct from every ``ModelCall`` outcome,
    which all describe an answer the model DID return.

    ``detail`` is what ``provider_calls.error_detail`` records (``PermissionDeniedError:403``),
    unchanged. ``message`` (the API's own words, at most ``MAX_FAILURE_MESSAGE_CHARS``),
    ``error_type`` and ``http_status`` are for the run log's ``model_call_failed`` row ONLY
    (spec 2026-10-03 §3.4): the reason a call was refused used to be recoverable only by a
    one-off probe."""

    def __init__(
        self,
        status: Literal["timeout", "error", "rate_limited"],
        detail: str,
        *,
        message: str | None = None,
        error_type: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.message = message
        self.error_type = error_type or detail.split(":", 1)[0]
        self.http_status = http_status


def _error_message(exc: Exception) -> str | None:
    """The API's own explanation, whitespace collapsed, at most ``MAX_FAILURE_MESSAGE_CHARS``:
    ``error.message`` of an Anthropic error body, else an AWS-style top-level ``message``, else
    the exception's own message."""
    message: object = None
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
        if not isinstance(message, str) or not message.strip():
            message = body.get("message", body.get("Message"))
    if not isinstance(message, str) or not message.strip():
        message = getattr(exc, "message", None) or str(exc)
    text = " ".join(str(message).split())
    return text[:MAX_FAILURE_MESSAGE_CHARS] or None


def _unavailable(
    status: Literal["timeout", "error", "rate_limited"], detail: str, exc: Exception
) -> ModelUnavailable:
    http_status = getattr(exc, "status_code", None)
    return ModelUnavailable(
        status,
        detail,
        message=_error_message(exc),
        error_type=type(exc).__name__,
        http_status=http_status if isinstance(http_status, int) else None,
    )


def _max_searches(tools: object) -> int | None:
    """The web-search tool's ``max_uses``, or None for a call without the tool."""
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and isinstance(tool.get("max_uses"), int):
                return int(tool["max_uses"])
    return None


class IntelModel(Protocol):
    async def extract(self, system: str, user: str) -> ModelCall[ExtractionOutput]: ...
    async def discover(self, system: str, user: str) -> ModelCall[DiscoveryOutput]: ...
    async def propose(self, system: str, user: str) -> ModelCall[ProposalOutput]: ...
    async def propose_sources(self, system: str, user: str) -> ModelCall[SourceProposalOutput]: ...
    async def search_once(self, system: str, user: str) -> ModelCall[DiscoveryOutput]: ...
    async def suggest_weights(self, system: str, user: str) -> ModelCall[SuggestionOutput]: ...


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
        # neither id this process sends changes mid-run.
        for model_id in (config.intel_extraction_model, config.intel_proposal_model):
            cost_of(model_id, Usage(0, 0, 0, 0, 0))
        self._config = config
        # Explicitly `Any`: an injected test double (SimpleNamespace) is not an
        # AsyncAnthropicAWS, and `self._client.messages.stream(**kwargs)` below
        # must not be checked against the real client's narrow model Literal.
        self._client: Any = client or anthropic.AsyncAnthropicAWS(
            aws_region=config.intel_anthropic_region,
            workspace_id=config.anthropic_aws_workspace_id,
            max_retries=0,
        )

    async def _send(
        self,
        output_format: type[T],
        *,
        model: str,
        effort: str | None = None,
        announce: bool = True,
        **kwargs: Any,
    ) -> ModelCall[T]:
        """One streamed API call, retried within ``_RETRIES``. ``announce`` writes the run log's
        ``model_call_started``; ``_search`` turns it off on a ``pause_turn`` resume, which its own
        ``continuing`` row announces instead."""
        started = time.monotonic()
        output_config = _output_config(output_format)
        if effort is not None:
            output_config["effort"] = effort
        run_log = current_run_log.get()
        if announce:
            await note_model_call_started(model, _max_searches(kwargs.get("tools")))
        response: Any = None
        for attempt in range(1, _RETRIES + 1):
            translator = StreamTranslator(run_log) if run_log is not None else None
            try:
                async with self._client.messages.stream(
                    model=model,
                    max_tokens=_MAX_TOKENS,
                    thinking=_THINKING,
                    output_config=output_config,
                    **kwargs,
                ) as stream:
                    async for event in stream:
                        if translator is not None:
                            await translator.feed(event)
                    response = await stream.get_final_message()
                break
            except anthropic.RateLimitError as exc:
                if attempt == _RETRIES:
                    raise _unavailable("rate_limited", type(exc).__name__, exc) from exc
            except anthropic.APITimeoutError as exc:
                raise _unavailable("timeout", type(exc).__name__, exc) from exc
            except anthropic.APIStatusError as exc:
                # A 4xx is final. A 5xx, or an `error` event inside an open stream (the stream's
                # own 200), is a server-side failure and retried.
                if 400 <= exc.status_code < 500 or attempt == _RETRIES:
                    raise _unavailable(
                        "error", f"{type(exc).__name__}:{exc.status_code}", exc
                    ) from exc
            except anthropic.APIConnectionError as exc:
                if attempt == _RETRIES:
                    raise _unavailable("error", type(exc).__name__, exc) from exc
            except httpx2.TimeoutException as exc:
                # Mid-body: the SDK wraps transport errors only before a response arrives.
                raise _unavailable("timeout", type(exc).__name__, exc) from exc
            except httpx2.TransportError as exc:
                if attempt == _RETRIES:
                    raise _unavailable("error", type(exc).__name__, exc) from exc
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
            cost_usd=cost_of(model, usage),
            latency_ms=int((time.monotonic() - started) * 1000),
            content=content,
        )

    async def extract(self, system: str, user: str) -> ModelCall[ExtractionOutput]:
        return await self._send(
            ExtractionOutput,
            model=self._config.intel_extraction_model,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

    async def _search(
        self,
        output_format: type[T],
        *,
        system: str,
        user: str,
        max_uses: int,
        effort: str | None = None,
    ) -> ModelCall[T]:
        """One web-search call on the extraction model, its pause_turn continuations driven to
        completion (spec §4.4), bounded by INTEL_MAX_CALLS_PER_RUN, usage summed across them.
        Discovery, source proposal and the validation search differ only in their schema,
        max_uses and ``effort`` (sent on the call and on every continuation of it)."""
        tools: list[dict[str, Any]] = [
            {
                "type": self._config.intel_web_search_tool_type,
                "name": "web_search",
                "max_uses": max_uses,
                **(
                    {"blocked_domains": self._config.intel_blocked_domains}
                    if self._config.intel_blocked_domains
                    else {}
                ),
            }
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        pause_turns = 0
        total_calls = 0  # every actual API call this search makes, initial included
        total = Usage(0, 0, 0, 0, 0)
        results: list[WebResult] = []
        while True:
            call = await self._send(
                output_format,
                model=self._config.intel_extraction_model,
                effort=effort,
                announce=pause_turns == 0,
                system=system,
                tools=tools,
                messages=messages,
            )
            total_calls += 1
            results.extend(web_results(call.content))
            u = call.usage
            total = Usage(
                total.input_tokens + u.input_tokens,
                total.output_tokens + u.output_tokens,
                total.cache_creation_input_tokens + u.cache_creation_input_tokens,
                total.cache_read_input_tokens + u.cache_read_input_tokens,
                total.web_search_requests + u.web_search_requests,
            )
            # `total_calls`, not `pause_turns`: the cap bounds every call this method makes, the
            # initial call included, checked before resuming again.
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
                    search_results=tuple(results),
                )
            pause_turns += 1
            await note_continuing(pause_turns)
            # Resume per Anthropic's stop-reason handling guidance: resend the original user turn
            # plus the paused assistant turn verbatim; the server resumes from the trailing
            # server_tool_use block.
            messages = [messages[0], {"role": "assistant", "content": call.content}]

    # INTEL_SEARCH_READ_EFFORT (spec 2026-10-03-intel-throughput §7) is sent on the two search
    # calls that READ or CHECK -- a saved search's read and stage 3's validation search -- and on
    # no other: not stage 1's source proposal, not extraction, proposal or suggestion.

    async def discover(self, system: str, user: str) -> ModelCall[DiscoveryOutput]:
        return await self._search(
            DiscoveryOutput,
            system=system,
            user=user,
            max_uses=self._config.intel_max_web_searches_per_run,
            effort=self._config.intel_search_read_effort,
        )

    async def propose_sources(self, system: str, user: str) -> ModelCall[SourceProposalOutput]:
        return await self._search(
            SourceProposalOutput,
            system=system,
            user=user,
            max_uses=self._config.intel_max_source_proposal_searches,
        )

    async def search_once(self, system: str, user: str) -> ModelCall[DiscoveryOutput]:
        return await self._search(
            DiscoveryOutput,
            system=system,
            user=user,
            max_uses=_VALIDATION_SEARCHES,
            effort=self._config.intel_search_read_effort,
        )

    async def propose(self, system: str, user: str) -> ModelCall[ProposalOutput]:
        return await self._send(
            ProposalOutput,
            model=self._config.intel_proposal_model,
            effort=_PROPOSAL_EFFORT,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

    async def suggest_weights(self, system: str, user: str) -> ModelCall[SuggestionOutput]:
        return await self._send(
            SuggestionOutput,
            model=self._config.intel_proposal_model,
            effort=_PROPOSAL_EFFORT,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
