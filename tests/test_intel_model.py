"""The model seam (task 8, spec §4.4/§5): schemas, prompts, pricing,
``ClaudeIntelModel`` and the stub. No network call, ever -- ``ClaudeIntelModel``
takes an injected client double whose ``messages.create`` is the only thing it
calls, matching ``anthropic.AsyncAnthropicAWS``'s shape.

Isolated from any developer ``.env.local`` the same way ``test_intel_config.py``
is: ``clean_env`` chdirs to a fresh ``tmp_path`` and deletes every key first.
"""

from __future__ import annotations

import inspect
import json
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from imageshield.intel import prompts
from imageshield.intel.config import IntelConfig
from imageshield.intel.model import ClaudeIntelModel, ModelUnavailable
from imageshield.intel.pricing import UnknownModelPrice, Usage, cost_of
from imageshield.intel.prompts import extraction_request
from imageshield.intel.schemas import (
    DiscoveryCandidate,
    DiscoveryOutput,
    ExtractionOutput,
    ProposalOutput,
)
from imageshield.intel.stub import StubIntelModel
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


class FakeMessages:
    """Stands in for ``client.messages`` -- only ``.create`` is exercised,
    never ``.parse`` (see model.py's module docstring for why)."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


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
    assert sent["thinking"] == {"type": "adaptive"}
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
        ' "rationale": "r", "signal_ids": []}], "coverage_gaps": []}'
    )
    assert parsed.weight_changes[0].delta == 7


async def test_the_stub_proposes_nothing() -> None:
    call = await StubIntelModel().propose("s", "u")
    assert call.outcome == "ok" and call.output == ProposalOutput()
