"""The model seam (task 8, spec §4.4/§5): schemas, prompts, pricing,
``ClaudeIntelModel`` and the stub. No network call, ever -- ``ClaudeIntelModel``
takes an injected client double whose ``messages.stream`` is the only thing it
calls, matching ``anthropic.AsyncAnthropicAWS``'s shape: an async context manager
whose stream yields events and then hands back the final message (every call is
streamed since 2026-10-03, the run log). The run-log tests at the end drive the
installed SDK's own ``AsyncMessageStream`` over scripted raw events, so the event
and block shapes the translator reads are anthropic 1.8.0's, not a guess.

Isolated from any developer ``.env.local`` the same way ``test_intel_config.py``
is: ``clean_env`` chdirs to a fresh ``tmp_path`` and deletes every key first.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import anthropic
import httpx2
import pytest
from anthropic._models import construct_type
from anthropic.lib.streaming import AsyncMessageStreamManager
from anthropic.types import RawMessageStreamEvent

from imageshield.intel import prompts
from imageshield.intel.config import IntelConfig
from imageshield.intel.model import ClaudeIntelModel, ModelUnavailable
from imageshield.intel.pricing import UnknownModelPrice, Usage, cost_of
from imageshield.intel.prompts import extraction_request
from imageshield.intel.run_log import RunLog, StreamTranslator, current_run_log
from imageshield.intel.schemas import (
    DiscoveryCandidate,
    DiscoveryOutput,
    ExtractionOutput,
    ProposalOutput,
    ProposedOptionSources,
    ProposedSource,
    SourceProposalOutput,
    SuggestionOutput,
)
from imageshield.intel.stub import StubIntelModel
from tests.intel_fakes import MemoryRunEvents
from tests.test_intel_config import BASE

# Not imported from test_intel_config: a `clean_env` parameter here would
# shadow that import (ruff F811) since pytest fixtures are looked up by
# parameter name, not by where they are defined. Duplicated instead of
# shared, same isolation this file's own fixture below gives every test:
# chdir to a fresh tmp_path and delete every IntelConfig-shaped key first, so
# a real .env.local above the repo root cannot silently refill one.
_ALL_ENV_NAMES = {name.upper() for name in IntelConfig.model_fields} | {
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    for key in _ALL_ENV_NAMES:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _usage(**kw: int) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=kw.get("i", 1000),
        output_tokens=kw.get("o", 100),
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=kw.get("ws", 0)),
    )


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


class _FakeStream:
    """What ``async with client.messages.stream(...)`` yields: events, then the final message.
    ``error`` is raised after the events, the way a stream breaks mid-body."""

    def __init__(self, events: list[Any], final: Any, error: Exception | None) -> None:
        self._events = events
        self._final = final
        self._error = error

    async def __aiter__(self) -> AsyncIterator[Any]:
        for event in self._events:
            yield event
        if self._error is not None:
            raise self._error

    async def get_final_message(self) -> Any:
        return self._final


class _FakeStreamManager:
    """``messages.stream(...)``'s return: an exception item is raised on entering, as a refused
    request is; anything else is the final message of a stream with no events."""

    def __init__(self, item: Any) -> None:
        self._item = item

    async def __aenter__(self) -> Any:
        if isinstance(self._item, Exception):
            raise self._item
        if isinstance(self._item, AsyncMessageStreamManager):
            return await self._item.__aenter__()
        return _FakeStream([], self._item, None)

    async def __aexit__(self, *exc: object) -> None:
        if isinstance(self._item, AsyncMessageStreamManager):
            await self._item.__aexit__(None, None, None)


class FakeMessages:
    """Stands in for ``client.messages`` -- only ``.stream`` is exercised, never ``.create`` or
    ``.parse`` (see model.py's module docstring for why). Each item is the final message of a
    call, an exception the call raises, or ``sdk_stream(...)``: the real SDK stream over
    scripted raw events."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _FakeStreamManager:
        self.calls.append(kwargs)
        return _FakeStreamManager(self._responses.pop(0))


def _model(
    responses: list[Any], clean_env: pytest.MonkeyPatch
) -> tuple[ClaudeIntelModel, FakeMessages]:
    for k, v in BASE.items():
        clean_env.setenv(k, v)
    from imageshield.intel.config import load_intel_config

    fake = FakeMessages(responses)
    return ClaudeIntelModel(load_intel_config(), client=SimpleNamespace(messages=fake)), fake


def test_cost_is_computed_from_usage_and_unknown_models_refuse() -> None:
    cost = cost_of("claude-sonnet-5", Usage(1_000_000, 0, 0, 0, 0))
    assert cost > Decimal("0")
    with pytest.raises(UnknownModelPrice):
        cost_of("claude-mystery-9", Usage(1, 1, 0, 0, 0))


def test_construction_refuses_an_unpriced_extraction_model(clean_env: pytest.MonkeyPatch) -> None:
    """Pricing is checked at CONSTRUCTION, not at the first billed call: an
    operator misconfiguring INTEL_EXTRACTION_MODEL fails the worker at startup,
    never mid-run after a call has already been made and billed (the gap Task 10
    flagged and this task's controller ruling closes)."""
    for k, v in BASE.items():
        clean_env.setenv(k, v)
    clean_env.setenv("INTEL_EXTRACTION_MODEL", "claude-mystery-9")
    from imageshield.intel.config import load_intel_config

    with pytest.raises(UnknownModelPrice):
        ClaudeIntelModel(load_intel_config(), client=SimpleNamespace(messages=FakeMessages([])))


async def test_an_unpriced_answering_model_is_still_priced_by_the_requested_model(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """The API may answer with a DIFFERENT model id than the one requested (a
    routing fallback). Pricing THAT id directly would raise UnknownModelPrice
    AFTER the call is already billed -- the exception would leave it unrecorded
    in provider_calls/provider_spend, and the run would retry (and re-bill) it up
    to MAX_RUN_ATTEMPTS times. The call is priced by the REQUESTED model instead
    -- the one this process always sends, and the one construction already
    proved is priced -- so a billed call is always recorded."""
    response = SimpleNamespace(
        model="claude-mystery-9",
        stop_reason="end_turn",
        usage=_usage(),
        content=[_text_block(ExtractionOutput(signals=[]).model_dump_json())],
    )
    model, _ = _model([response], clean_env)
    call = await model.extract("sys", "user")
    assert call.answered_by == "claude-mystery-9"
    assert call.cost_usd == cost_of("claude-sonnet-5", Usage(1000, 100, 0, 0, 0))


async def test_a_parsed_response_is_ok(clean_env: pytest.MonkeyPatch) -> None:
    parsed = ExtractionOutput(signals=[])
    response = SimpleNamespace(
        model="claude-sonnet-5",
        stop_reason="end_turn",
        usage=_usage(),
        content=[_text_block(parsed.model_dump_json())],
    )
    model, fake = _model([response], clean_env)
    call = await model.extract("sys", "user")
    assert call.outcome == "ok" and call.output == parsed and call.answered_by == "claude-sonnet-5"
    sent = fake.calls[0]
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["output_config"]["format"]["schema"]["title"] == "ExtractionOutput"
    assert call.cost_usd > Decimal("0")


@pytest.mark.parametrize(
    ("stop", "outcome"), [("refusal", "refusal"), ("max_tokens", "max_tokens")]
)
async def test_refusal_and_max_tokens_are_neutral_outcomes(
    stop: str, outcome: str, clean_env: pytest.MonkeyPatch
) -> None:
    response = SimpleNamespace(
        model="claude-sonnet-5", stop_reason=stop, usage=_usage(), content=[]
    )
    model, _ = _model([response], clean_env)
    call = await model.extract("sys", "user")
    assert call.outcome == outcome and call.output is None
    # Usage/cost are still recorded on a neutral outcome -- the billed call
    # must never disappear from metering just because nothing was parsed.
    assert call.cost_usd > Decimal("0")


async def test_an_unparseable_response_is_neutral(clean_env: pytest.MonkeyPatch) -> None:
    response = SimpleNamespace(
        model="claude-sonnet-5",
        stop_reason="end_turn",
        usage=_usage(),
        content=[_text_block("not json")],
    )
    model, _ = _model([response], clean_env)
    call = await model.extract("sys", "user")
    assert call.outcome == "unparseable" and call.output is None
    assert call.cost_usd > Decimal("0")


async def test_a_response_with_no_text_block_is_unparseable(clean_env: pytest.MonkeyPatch) -> None:
    response = SimpleNamespace(
        model="claude-sonnet-5", stop_reason="end_turn", usage=_usage(), content=[]
    )
    model, _ = _model([response], clean_env)
    assert (await model.extract("sys", "user")).outcome == "unparseable"


async def test_transport_failures_raise_model_unavailable(clean_env: pytest.MonkeyPatch) -> None:
    import anthropic  # tests may import it; src may not outside model.py
    import httpx

    err = anthropic.APITimeoutError(request=httpx.Request("POST", "https://x"))
    model, _ = _model([err], clean_env)
    with pytest.raises(ModelUnavailable) as caught:
        await model.extract("sys", "user")
    assert caught.value.status == "timeout"


def test_prompts_carry_no_person_fields() -> None:
    system, user = extraction_request(
        "text",
        source_kind="news",
        tag_hints=("instagram",),
        registry_tags=[{"slug": "instagram", "label": "Instagram", "description": "photo app"}],
    )
    assert "user_ref" not in system + user and "phone" not in (system + user).lower()


_PERSON_SHAPED = re.compile(
    r"\b(user_ref|person\w*|answers?|phone\w*|email\w*|name)\b", re.IGNORECASE
)


def test_prompt_builders_take_no_person_shaped_parameter() -> None:
    """spec §4.4: prompt inputs are public document text, operator queries and
    the published tag registry -- never a person (INVARIANTS #48). Walking
    every function DEFINED in prompts.py, rather than naming them, means a
    third builder added later is covered by construction rather than by
    somebody remembering to extend this test."""
    builders = [
        obj
        for obj in vars(prompts).values()
        if inspect.isfunction(obj) and obj.__module__ == prompts.__name__
    ]
    assert len(builders) >= 2, "expected at least extraction_request and discovery_request"
    for builder in builders:
        for param in inspect.signature(builder).parameters.values():
            annotation = (
                "" if param.annotation is inspect.Parameter.empty else str(param.annotation)
            )
            assert not _PERSON_SHAPED.search(param.name), (
                f"{builder.__name__}: param {param.name!r}"
            )
            assert not _PERSON_SHAPED.search(annotation), (
                f"{builder.__name__}: param {param.name!r} annotated {annotation!r}"
            )


async def test_the_stub_proposes_nothing_and_says_so() -> None:
    call = await StubIntelModel().extract("s", "u")
    assert call.outcome == "ok" and call.output is not None and call.output.signals == []
    assert call.answered_by == "stub" and call.cost_usd == Decimal("0")


def _paused(**usage: int) -> SimpleNamespace:
    block = SimpleNamespace(
        type="server_tool_use", id="srvtoolu_1", name="web_search", input={"query": "q"}
    )
    return SimpleNamespace(
        model="claude-sonnet-5", stop_reason="pause_turn", usage=_usage(**usage), content=[block]
    )


async def test_discover_returns_every_results_page_age_across_its_continuations(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """spec 2026-10-04-intel-evidence-quality §2: the search's own page_age for each result, from
    the paused leg and the final one, rides back on the call. The SDK's ``WebSearchResultBlock``
    carries ``page_age`` (anthropic 1.8.0); nothing else of the result is kept."""
    def results(*pairs: tuple[str, str | None]) -> SimpleNamespace:
        return SimpleNamespace(
            type="web_search_tool_result",
            tool_use_id="srvtoolu_1",
            content=[
                SimpleNamespace(type="web_search_result", url=u, page_age=a, title="t")
                for u, a in pairs
            ],
        )

    paused = _paused(ws=1)
    paused.content = [*paused.content, results(("https://n.example/a", "3 days ago"))]
    output = DiscoveryOutput(candidates=[DiscoveryCandidate(url="https://n.example/a", reason="r")])
    final = SimpleNamespace(
        model="claude-sonnet-5",
        stop_reason="end_turn",
        usage=_usage(ws=1),
        content=[
            results(("https://n.example/b", "April 30, 2025"), ("https://n.example/c", None)),
            _text_block(output.model_dump_json()),
        ],
    )
    model, _ = _model([paused, final], clean_env)
    call = await model.discover("sys", "find things")
    assert [(r.url, r.page_age) for r in call.search_results] == [
        ("https://n.example/a", "3 days ago"),
        ("https://n.example/b", "April 30, 2025"),
        ("https://n.example/c", None),
    ]


async def test_discover_resumes_a_pause_turn_with_the_first_user_turn_and_the_paused_content(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """spec §4.4: a web-search ``pause_turn`` is driven to completion inside
    discover(). The resume resends the ORIGINAL user turn plus the paused assistant
    turn verbatim (``call.content``), and usage accumulates across both calls, so
    what metering records is the whole search, not its last leg."""
    paused = _paused(i=100, o=10, ws=1)
    output = DiscoveryOutput(candidates=[DiscoveryCandidate(url="https://n.example/a", reason="r")])
    final = SimpleNamespace(
        model="claude-sonnet-5",
        stop_reason="end_turn",
        usage=_usage(i=200, o=20, ws=2),
        content=[_text_block(output.model_dump_json())],
    )
    model, fake = _model([paused, final], clean_env)
    call = await model.discover("sys", "find things")
    assert call.outcome == "ok" and call.output == output and call.pause_turns == 1
    first, second = fake.calls
    assert first["messages"] == [{"role": "user", "content": "find things"}]
    assert second["messages"] == [
        {"role": "user", "content": "find things"},
        {"role": "assistant", "content": paused.content},
    ]
    assert second["tools"] == first["tools"] and second["system"] == "sys"
    assert call.usage == Usage(300, 30, 0, 0, 3)
    assert call.cost_usd == cost_of("claude-sonnet-5", Usage(300, 30, 0, 0, 3))


async def test_discover_stops_resuming_at_the_call_cap(clean_env: pytest.MonkeyPatch) -> None:
    """A search that never stops pausing ends -- it does not loop forever, and it
    never makes MORE than the configured cap of actual API calls (the off-by-one
    Task 10 flagged: the initial call must count against its own cap, not only
    the resumes after it). It comes back unparseable (a pause carries no text
    block), a neutral verdict."""
    clean_env.setenv("INTEL_MAX_CALLS_PER_RUN", "2")
    model, fake = _model([_paused() for _ in range(5)], clean_env)
    call = await model.discover("sys", "find things")
    assert call.pause_turns == 1 and len(fake.calls) == 2
    assert call.outcome == "unparseable" and call.output is None


async def test_discover_never_exceeds_the_cap_for_any_cap_value(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Same property, pinned at the boundary: a cap of 1 must still allow the
    FIRST call (nothing has been called yet) but never a resume."""
    clean_env.setenv("INTEL_MAX_CALLS_PER_RUN", "1")
    model, fake = _model([_paused() for _ in range(5)], clean_env)
    call = await model.discover("sys", "find things")
    assert call.pause_turns == 0 and len(fake.calls) == 1


async def test_propose_uses_the_proposal_model_with_explicit_effort(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """spec §4.3: Opus 5.5 defaults to effort medium, so the proposal call sets it.
    Priced by the REQUESTED proposal model, never the answering id."""
    response = SimpleNamespace(
        model="claude-opus-5-5",
        stop_reason="end_turn",
        usage=_usage(),
        content=[_text_block(ProposalOutput().model_dump_json())],
    )
    model, fake = _model([response], clean_env)
    call = await model.propose("sys", "user")
    assert call.outcome == "ok" and call.output == ProposalOutput()
    sent = fake.calls[0]
    assert sent["model"] == "claude-opus-5-5"
    assert sent["output_config"]["effort"] == "high"
    # The run log shows Claude's reasoning summary (spec 2026-10-03 §2).
    assert sent["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert call.cost_usd == cost_of("claude-opus-5-5", Usage(1000, 100, 0, 0, 0))


async def test_extraction_keeps_its_model_and_sets_no_effort(
    clean_env: pytest.MonkeyPatch,
) -> None:
    response = SimpleNamespace(
        model="claude-sonnet-5",
        stop_reason="end_turn",
        usage=_usage(),
        content=[_text_block(ExtractionOutput(signals=[]).model_dump_json())],
    )
    model, fake = _model([response], clean_env)
    await model.extract("sys", "user")
    assert fake.calls[0]["model"] == "claude-sonnet-5"
    assert "effort" not in fake.calls[0]["output_config"]


def test_construction_refuses_an_unpriced_proposal_model(clean_env: pytest.MonkeyPatch) -> None:
    for k, v in BASE.items():
        clean_env.setenv(k, v)
    clean_env.setenv("INTEL_PROPOSAL_MODEL", "claude-mystery-9")
    from imageshield.intel.config import load_intel_config

    with pytest.raises(UnknownModelPrice):
        ClaudeIntelModel(load_intel_config(), client=SimpleNamespace(messages=FakeMessages([])))


def test_the_proposal_schema_carries_no_numeric_or_length_bounds() -> None:
    """Review Focus 1: structured output does not enforce minimum/maximum/maxLength and the
    SDK would validate them client-side, failing the WHOLE response over one bad delta.
    §4.5's bounds run per proposal, in code (intel/generation.py)."""
    schema = json.dumps(ProposalOutput.model_json_schema())
    for keyword in ("minimum", "maximum", "maxLength", "minLength"):
        assert keyword not in schema
    parsed = ProposalOutput.model_validate_json(
        '{"weight_changes": [{"question_key": "q", "option": "o", "current": 3, "delta": 7,'
        ' "body": "b",'
        ' "rationale": "r", "signal_ids": []}], "coverage_gaps": []}'
    )
    assert parsed.weight_changes[0].delta == 7


def test_the_threat_output_parses_out_of_range_numbers_for_code_to_drop() -> None:
    """Review Focus 1 of step 2, extended: no numeric bounds in the schema, so one bad severity
    reaches intel/generation.py, which drops that one proposal, instead of failing the whole
    response in the SDK."""
    schema = json.dumps(ProposalOutput.model_json_schema())
    for keyword in ("minimum", "maximum", "maxLength", "minLength"):
        assert keyword not in schema
    parsed = ProposalOutput.model_validate_json(
        '{"threat_events": [{"kind": "leak", "title": "t", "body": "b", "severity": 9,'
        ' "expires_in_days": 400, "tags": [], "rationale": "r", "signal_ids": []}],'
        ' "attach": [{"proposal_id": "not-a-uuid", "signal_ids": []}]}'
    )
    assert parsed.threat_events[0].severity == 9
    assert parsed.attach[0].proposal_id == "not-a-uuid"


async def test_the_stub_proposes_nothing() -> None:
    call = await StubIntelModel().propose("s", "u")
    assert call.outcome == "ok" and call.output == ProposalOutput()


def test_the_protection_output_parses_out_of_range_numbers_for_code_to_drop() -> None:
    """Review Focus 1 of step 2, extended to step 4: no numeric bounds in the schema, so a
    strength of 9 or a global claim reaches intel/generation.py, which drops that one proposal
    instead of failing the whole response in the SDK."""
    schema = json.dumps(ProposalOutput.model_json_schema())
    for keyword in ("minimum", "maximum", "maxLength", "minLength"):
        assert keyword not in schema
    assert "protection_events" in schema
    parsed = ProposalOutput.model_validate_json(
        '{"protection_events": [{"title": "t", "body": "b", "strength": 9, "review_in_days": 4000,'
        ' "tags": [], "is_global": true, "rationale": "r", "signal_ids": []}]}'
    )
    (proposed,) = parsed.protection_events
    assert proposed.strength == 9 and proposed.is_global is True


async def test_discover_still_searches_with_the_per_run_budget(
    clean_env: pytest.MonkeyPatch,
) -> None:
    response = SimpleNamespace(
        model="claude-sonnet-5",
        stop_reason="end_turn",
        usage=_usage(ws=1),
        content=[_text_block(DiscoveryOutput(candidates=[]).model_dump_json())],
    )
    model, fake = _model([response], clean_env)
    await model.discover("sys", "q")
    (tool,) = fake.calls[0]["tools"]
    assert tool["max_uses"] == 5 and tool["type"] == "web_search_20260209"


async def test_propose_sources_searches_with_its_own_budget_on_the_extraction_model(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """spec §4.10 stage 1: one INTEL_EXTRACTION_MODEL call with web search, max_uses =
    INTEL_MAX_SOURCE_PROPOSAL_SEARCHES."""
    for k, v in BASE.items():
        clean_env.setenv(k, v)
    clean_env.setenv("INTEL_MAX_SOURCE_PROPOSAL_SEARCHES", "7")
    from imageshield.intel.config import load_intel_config

    output = SourceProposalOutput(
        options=[
            ProposedOptionSources(
                option="Instagram",
                candidates=[
                    ProposedSource(
                        kind="policy_page", source_url="https://p.example/terms", reason="terms"
                    )
                ],
            )
        ]
    )
    response = SimpleNamespace(
        model="claude-sonnet-5",
        stop_reason="end_turn",
        usage=_usage(ws=2),
        content=[_text_block(output.model_dump_json())],
    )
    fake = FakeMessages([response])
    model = ClaudeIntelModel(load_intel_config(), client=SimpleNamespace(messages=fake))
    call = await model.propose_sources("sys", "question")
    assert call.outcome == "ok" and call.output == output
    sent = fake.calls[0]
    assert sent["model"] == "claude-sonnet-5" and "effort" not in sent["output_config"]
    assert sent["tools"][0]["max_uses"] == 7
    # sources-v2 (spec 2026-10-08): its own output budget, twice every other call's.
    assert sent["max_tokens"] == 16000
    assert call.cost_usd == cost_of("claude-sonnet-5", Usage(1000, 100, 0, 0, 2))


async def test_search_once_allows_exactly_one_search(clean_env: pytest.MonkeyPatch) -> None:
    """spec §4.10 stage 3: a search_query candidate costs ONE web search."""
    output = DiscoveryOutput(candidates=[DiscoveryCandidate(url="https://n.example/a", reason="r")])
    response = SimpleNamespace(
        model="claude-sonnet-5",
        stop_reason="end_turn",
        usage=_usage(ws=1),
        content=[_text_block(output.model_dump_json())],
    )
    model, fake = _model([response], clean_env)
    call = await model.search_once("sys", '{"query": "q"}')
    assert call.output == output
    assert fake.calls[0]["tools"][0]["max_uses"] == 1
    assert fake.calls[0]["model"] == "claude-sonnet-5"


async def test_suggest_weights_uses_the_proposal_model_with_explicit_effort(
    clean_env: pytest.MonkeyPatch,
) -> None:
    response = SimpleNamespace(
        model="claude-opus-5-5",
        stop_reason="end_turn",
        usage=_usage(),
        content=[_text_block(SuggestionOutput().model_dump_json())],
    )
    model, fake = _model([response], clean_env)
    call = await model.suggest_weights("sys", "user")
    assert call.outcome == "ok" and call.output == SuggestionOutput()
    sent = fake.calls[0]
    assert sent["model"] == "claude-opus-5-5" and sent["output_config"]["effort"] == "high"
    assert "tools" not in sent


def test_the_step5_schemas_carry_no_numeric_or_length_bounds() -> None:
    """The §4.5 rule for step 5: one bad candidate or option must not make the whole response
    unparseable. intel/source_choice.py and intel/suggestion.py bound them in code."""
    for output_model in (SourceProposalOutput, SuggestionOutput):
        schema = json.dumps(output_model.model_json_schema())
        for keyword in ("minimum", "maximum", "maxLength", "minLength"):
            assert keyword not in schema
    parsed = SuggestionOutput.model_validate_json('{"options": [{"option": "o", "deduction": 14}]}')
    assert parsed.options[0].deduction == 14


async def test_the_stub_answers_the_step5_calls_with_nothing() -> None:
    stub = StubIntelModel()
    assert (await stub.propose_sources("s", "u")).output == SourceProposalOutput()
    assert (await stub.search_once("s", "u")).output == DiscoveryOutput(candidates=[])
    assert (await stub.suggest_weights("s", "u")).output == SuggestionOutput()


# ── the run log: streaming translation (spec 2026-10-03 §3.4) ─────────────────────────────────


class _RawStream:
    """Stands in for the SDK's ``AsyncStream[RawMessageStreamEvent]``: the raw events the API
    sends, as the SDK's own models, so ``AsyncMessageStream`` accumulates and fires its events
    exactly as it does against the wire."""

    def __init__(self, raw: list[dict[str, Any]], error: Exception | None) -> None:
        self._events = [construct_type(type_=RawMessageStreamEvent, value=event) for event in raw]
        self._error = error

    async def __aiter__(self) -> AsyncIterator[Any]:
        for event in self._events:
            yield event
        if self._error is not None:
            raise self._error

    async def close(self) -> None:
        return None


def sdk_stream(
    raw: list[dict[str, Any]], error: Exception | None = None
) -> AsyncMessageStreamManager[Any]:
    async def request() -> Any:
        return _RawStream(raw, error)

    return AsyncMessageStreamManager(request())


def _message_start(model: str = "claude-sonnet-5", i: int = 1000) -> dict[str, Any]:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": i, "output_tokens": 1},
        },
    }


def _block(index: int, block: dict[str, Any], *deltas: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"type": "content_block_start", "index": index, "content_block": block},
        *({"type": "content_block_delta", "index": index, "delta": d} for d in deltas),
        {"type": "content_block_stop", "index": index},
    ]


def _message_end(
    stop: str = "end_turn", *, i: int = 1000, o: int = 100, ws: int = 0
) -> list[dict[str, Any]]:
    return [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {
                "input_tokens": i,
                "output_tokens": o,
                "server_tool_use": {"web_search_requests": ws},
            },
        },
        {"type": "message_stop"},
    ]


def _thinking(index: int, *parts: str) -> list[dict[str, Any]]:
    return _block(
        index,
        {"type": "thinking", "thinking": "", "signature": ""},
        *({"type": "thinking_delta", "thinking": part} for part in parts),
        {"type": "signature_delta", "signature": "sig"},
    )


def _search(index: int, query: str) -> list[dict[str, Any]]:
    encoded = json.dumps({"query": query})
    half = len(encoded) // 2
    return _block(
        index,
        {"type": "server_tool_use", "id": f"srvtoolu_{index}", "name": "web_search", "input": {}},
        {"type": "input_json_delta", "partial_json": encoded[:half]},
        {"type": "input_json_delta", "partial_json": encoded[half:]},
    )


def _results(index: int, *urls: str) -> list[dict[str, Any]]:
    content = [
        {
            "type": "web_search_result",
            "title": f"Result {n}",
            "url": url,
            "encrypted_content": "opaque",
            "page_age": None,
        }
        for n, url in enumerate(urls)
    ]
    block = {
        "type": "web_search_tool_result",
        "tool_use_id": f"srvtoolu_{index - 1}",
        "content": content,
    }
    return _block(index, block)


def _search_error(index: int, code: str) -> list[dict[str, Any]]:
    return _block(
        index,
        {
            "type": "web_search_tool_result",
            "tool_use_id": f"srvtoolu_{index - 1}",
            "content": {"type": "web_search_tool_result_error", "error_code": code},
        },
    )


def _text(index: int, text: str) -> list[dict[str, Any]]:
    return _block(index, {"type": "text", "text": ""}, {"type": "text_delta", "text": text})


@pytest.fixture
def bound_log() -> Iterator[tuple[RunLog, MemoryRunEvents]]:
    store = MemoryRunEvents()
    run_log = RunLog(uuid4(), store)
    token = current_run_log.set(run_log)
    try:
        yield run_log, store
    finally:
        current_run_log.reset(token)


_FOUND = DiscoveryOutput(candidates=[DiscoveryCandidate(url="https://n.example/a", reason="r")])


async def test_a_streamed_search_becomes_its_steps(
    clean_env: pytest.MonkeyPatch, bound_log: tuple[RunLog, MemoryRunEvents]
) -> None:
    """One call, read off the installed SDK's own stream: ONE thinking row grown to the whole
    summary, each search with its query (complete only at its block's stop), each result list
    with titles and URLs, a search error by its code, and `writing` once, at the FIRST text
    block."""
    run_log, store = bound_log
    raw = [
        _message_start(),
        *_thinking(0, "Looking for ", "recent breaches ", "on dating apps."),
        *_text(1, "Let me search."),
        *_search(2, "bumble data breach 2026"),
        *_results(
            3,
            "https://www.bumble.com/en/news",
            "https://techcrunch.com/a",
            "https://www.theverge.com/b",
            "https://techcrunch.com/c",
        ),
        *_search(4, "bumble leak"),
        *_search_error(5, "max_uses_exceeded"),
        *_text(6, _FOUND.model_dump_json()),
        *_message_end(ws=2),
    ]
    model, _ = _model([sdk_stream(raw)], clean_env)
    call = await model.discover("sys", "find things")
    assert call.outcome == "ok" and call.output == _FOUND

    events = store.events(run_log.run_id)
    assert [e["kind"] for e in events] == [
        "model_call_started",
        "thinking",
        "writing",
        "search",
        "search_results",
        "search",
        "search_results",
    ]
    started, thinking, writing, search, results, _, failed = events
    assert started["text"] == "Asked claude-sonnet-5 (up to 5 web searches)"
    assert started["detail"] == {"model": "claude-sonnet-5", "max_searches": 5}
    summary = "Looking for recent breaches on dating apps."
    assert thinking["text"] == summary and thinking["detail"] == {"chars": len(summary)}
    assert writing["text"] == "Writing the answer" and writing["detail"] == {}
    assert search["text"] == "Searched: bumble data breach 2026"
    assert search["detail"] == {"query": "bumble data breach 2026"}
    assert results["text"] == "4 results: bumble.com, techcrunch.com, theverge.com"
    assert results["detail"]["count"] == 4 and results["detail"]["error_code"] is None
    assert results["detail"]["results"][0] == {
        "title": "Result 0",
        "url": "https://www.bumble.com/en/news",
    }
    assert failed["text"] == "Search failed: max_uses_exceeded"
    assert failed["detail"] == {"count": 0, "results": [], "error_code": "max_uses_exceeded"}


async def test_a_call_without_the_search_tool_is_announced_without_a_limit(
    clean_env: pytest.MonkeyPatch, bound_log: tuple[RunLog, MemoryRunEvents]
) -> None:
    run_log, store = bound_log
    raw = [
        _message_start("claude-opus-5-5"),
        *_text(0, ProposalOutput().model_dump_json()),
        *_message_end(),
    ]
    model, fake = _model([sdk_stream(raw)], clean_env)
    await model.propose("sys", "user")
    (started, writing) = store.events(run_log.run_id)
    assert started["text"] == "Asked claude-opus-5-5"
    assert started["detail"] == {"model": "claude-opus-5-5", "max_searches": None}
    assert writing["kind"] == "writing"
    assert fake.calls[0]["thinking"] == {"type": "adaptive", "display": "summarized"}


async def test_a_pause_turn_resume_is_continuing_not_a_second_question(
    clean_env: pytest.MonkeyPatch, bound_log: tuple[RunLog, MemoryRunEvents]
) -> None:
    """The resume is announced as `continuing`; the call itself was announced once."""
    run_log, store = bound_log
    paused = [
        _message_start(i=100),
        *_search(0, "first query"),
        *_results(1, "https://a.example/x"),
        *_message_end("pause_turn", i=100, o=10, ws=1),
    ]
    final = [
        _message_start(i=200),
        *_search(0, "second query"),
        *_results(1, "https://b.example/y"),
        *_text(2, _FOUND.model_dump_json()),
        *_message_end(i=200, o=20, ws=1),
    ]
    model, fake = _model([sdk_stream(paused), sdk_stream(final)], clean_env)
    call = await model.discover("sys", "find things")
    assert call.pause_turns == 1 and call.output == _FOUND
    assert call.usage == Usage(300, 30, 0, 0, 2)
    assert store.kinds(run_log.run_id) == [
        "model_call_started",
        "search",
        "search_results",
        "continuing",
        "search",
        "search_results",
        "writing",
    ]
    (continuing,) = [e for e in store.events(run_log.run_id) if e["kind"] == "continuing"]
    assert continuing["text"] == "Continuing — Claude paused after its searches"
    assert continuing["detail"] == {"pause_turns": 1}
    # The paused turn is resent as the SDK accumulated it.
    resent = fake.calls[1]["messages"][1]["content"]
    assert [block.type for block in resent] == ["server_tool_use", "web_search_tool_result"]


async def test_the_streamed_call_equals_the_call_today_for_the_same_message(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Same final message, same ModelCall: what `create` returned and what the stream
    accumulates are classified, priced and counted identically."""
    text = _FOUND.model_dump_json()
    raw = [_message_start(), *_text(0, text), *_message_end(o=100, ws=1)]
    created = SimpleNamespace(
        model="claude-sonnet-5",
        stop_reason="end_turn",
        usage=_usage(i=1000, o=100, ws=1),
        content=[_text_block(text)],
    )
    streamed_model, _ = _model([sdk_stream(raw)], clean_env)
    created_model, _ = _model([created], clean_env)
    streamed = await streamed_model.search_once("sys", "q")
    today = await created_model.search_once("sys", "q")
    for name in ("output", "outcome", "answered_by", "stop_reason", "usage", "cost_usd"):
        assert getattr(streamed, name) == getattr(today, name), name
    assert streamed.pause_turns == today.pause_turns == 0


def _status_error(cls: type[anthropic.APIStatusError], status: int, body: Any) -> Any:
    request = httpx2.Request("POST", "https://anthropic.aws.example/v1/messages")
    return cls("Error code", response=httpx2.Response(status, request=request), body=body)


async def test_a_403_from_the_stream_is_unavailable_exactly_as_before_and_names_the_reason(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """`detail` is what provider_calls.error_detail records, unchanged; the API's own message
    rides on the exception for the run log only."""
    reason = "User is not authorized to perform: sts:GetWebIdentityToken"
    refused = _status_error(
        anthropic.PermissionDeniedError,
        403,
        {"type": "error", "error": {"type": "permission_error", "message": reason}},
    )
    model, fake = _model([refused], clean_env)
    with pytest.raises(ModelUnavailable) as caught:
        await model.propose("sys", "user")
    assert (caught.value.status, caught.value.detail) == ("error", "PermissionDeniedError:403")
    assert caught.value.message == reason
    assert caught.value.http_status == 403 and caught.value.error_type == "PermissionDeniedError"
    assert len(fake.calls) == 1  # a 4xx is never retried


async def test_an_aws_style_body_and_a_long_message_are_read_and_bounded(
    clean_env: pytest.MonkeyPatch,
) -> None:
    long = "not authorized " * 100
    refused = _status_error(anthropic.PermissionDeniedError, 403, {"Message": long})
    model, _ = _model([refused], clean_env)
    with pytest.raises(ModelUnavailable) as caught:
        await model.extract("sys", "user")
    assert caught.value.message is not None and len(caught.value.message) == 500
    assert caught.value.message.startswith("not authorized not authorized")


async def test_an_error_event_inside_an_open_stream_is_retried_like_a_5xx(
    clean_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `error` event mid-stream arrives as an APIStatusError carrying the stream's own 200:
    a server-side failure (overloaded_error), so the next attempt runs."""
    import imageshield.intel.model as model_module

    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(model_module, "asyncio", SimpleNamespace(sleep=_no_sleep))
    overloaded = _status_error(
        anthropic.APIStatusError,
        200,
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    )
    good = [_message_start(), *_text(0, ExtractionOutput(signals=[]).model_dump_json())]
    model, fake = _model(
        [sdk_stream([_message_start()], overloaded), sdk_stream([*good, *_message_end()])],
        clean_env,
    )
    call = await model.extract("sys", "user")
    assert call.outcome == "ok" and len(fake.calls) == 2


async def test_a_transport_timeout_mid_body_is_a_timeout(clean_env: pytest.MonkeyPatch) -> None:
    """The SDK wraps transport errors only before a response arrives; one mid-body is mapped
    the way the SDK maps the same error before one."""
    request = httpx2.Request("POST", "https://anthropic.aws.example/v1/messages")
    model, _ = _model(
        [sdk_stream([_message_start()], httpx2.ReadTimeout("read timed out", request=request))],
        clean_env,
    )
    with pytest.raises(ModelUnavailable) as caught:
        await model.extract("sys", "user")
    assert (caught.value.status, caught.value.detail) == ("timeout", "ReadTimeout")


async def test_a_log_that_cannot_be_written_changes_nothing_about_the_call(
    clean_env: pytest.MonkeyPatch,
) -> None:
    raw = [
        _message_start(),
        *_thinking(0, "Thinking it through."),
        *_search(1, "q"),
        *_results(2, "https://a.example/x"),
        *_text(3, _FOUND.model_dump_json()),
        *_message_end(ws=1),
    ]
    unbound_model, _ = _model([sdk_stream(raw)], clean_env)
    unbound = await unbound_model.discover("sys", "find things")
    broken = MemoryRunEvents(fail=True)
    token = current_run_log.set(RunLog(uuid4(), broken))
    try:
        logged_model, _ = _model([sdk_stream(raw)], clean_env)
        logged = await logged_model.discover("sys", "find things")
    finally:
        current_run_log.reset(token)
    for name in ("output", "outcome", "answered_by", "stop_reason", "usage", "cost_usd"):
        assert getattr(logged, name) == getattr(unbound, name), name
    assert broken.rows == {}


async def test_thinking_is_rewritten_at_most_every_two_seconds() -> None:
    """One row per thinking block: written at its start, rewritten while deltas arrive no more
    often than THINKING_UPDATE_SECONDS, and finalised at its stop."""
    store = MemoryRunEvents()
    run_log = RunLog(uuid4(), store)
    now = [0.0]
    translator = StreamTranslator(run_log, clock=lambda: now[0])
    block = SimpleNamespace(type="thinking", thinking="", signature="")
    await translator.feed(SimpleNamespace(type="content_block_start", index=0, content_block=block))
    for at, snapshot in ((0.5, "a"), (1.9, "ab"), (2.1, "abc"), (3.0, "abcd"), (4.2, "abcde")):
        now[0] = at
        await translator.feed(SimpleNamespace(type="thinking", thinking="x", snapshot=snapshot))
    assert store.updates == 2  # at 2.1s and at 4.2s
    final = SimpleNamespace(type="thinking", thinking="abcdef", signature="s")
    await translator.feed(SimpleNamespace(type="content_block_stop", index=0, content_block=final))
    (row,) = store.events(run_log.run_id)
    assert row["kind"] == "thinking" and row["text"] == "abcdef" and row["detail"] == {"chars": 6}


async def test_an_unreadable_event_is_skipped_never_raised() -> None:
    store = MemoryRunEvents()
    translator = StreamTranslator(RunLog(uuid4(), store))
    await translator.feed(SimpleNamespace(type="content_block_stop", content_block=object()))
    await translator.feed(object())
    assert store.rows == {}


# ── Claude's effort on search READS (spec 2026-10-03-intel-throughput §7) ─────────────────────


def _model_at_effort(
    responses: list[Any], clean_env: pytest.MonkeyPatch, effort: str
) -> tuple[ClaudeIntelModel, FakeMessages]:
    """``_model`` with INTEL_SEARCH_READ_EFFORT set over BASE's."""
    for k, v in BASE.items():
        clean_env.setenv(k, v)
    clean_env.setenv("INTEL_SEARCH_READ_EFFORT", effort)
    from imageshield.intel.config import load_intel_config

    fake = FakeMessages(responses)
    return ClaudeIntelModel(load_intel_config(), client=SimpleNamespace(messages=fake)), fake


def _discovered(ws: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        model="claude-sonnet-5",
        stop_reason="end_turn",
        usage=_usage(ws=ws),
        content=[_text_block(DiscoveryOutput(candidates=[]).model_dump_json())],
    )


async def test_a_saved_searchs_read_sends_the_search_read_effort_on_every_call(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """BASE sets INTEL_SEARCH_READ_EFFORT=medium. A pause_turn resume is the same search, so it
    carries the same effort."""
    model, fake = _model([_paused(), _discovered()], clean_env)
    call = await model.discover("sys", "q")
    assert call.pause_turns == 1 and len(fake.calls) == 2
    assert [sent["output_config"]["effort"] for sent in fake.calls] == ["medium", "medium"]


async def test_a_validation_search_sends_the_search_read_effort(
    clean_env: pytest.MonkeyPatch,
) -> None:
    model, fake = _model_at_effort([_discovered()], clean_env, "low")
    await model.search_once("sys", '{"query": "q"}')
    assert fake.calls[0]["output_config"]["effort"] == "low"


async def test_no_other_call_carries_the_search_read_effort(clean_env: pytest.MonkeyPatch) -> None:
    """Stage 1's source proposal searches too, but it PROPOSES, so it keeps the model's own
    default; extraction sends none; proposal and suggestion keep their explicit 'high'."""

    def answer(output: Any) -> SimpleNamespace:
        return SimpleNamespace(
            model="claude-sonnet-5",
            stop_reason="end_turn",
            usage=_usage(),
            content=[_text_block(output.model_dump_json())],
        )

    model, fake = _model_at_effort(
        [
            answer(SourceProposalOutput()),
            answer(ExtractionOutput(signals=[])),
            answer(ProposalOutput()),
            answer(SuggestionOutput()),
        ],
        clean_env,
        "low",
    )
    await model.propose_sources("sys", "question")
    await model.extract("sys", "user")
    await model.propose("sys", "user")
    await model.suggest_weights("sys", "user")
    efforts = [sent["output_config"].get("effort") for sent in fake.calls]
    assert efforts == [None, None, "high", "high"]
