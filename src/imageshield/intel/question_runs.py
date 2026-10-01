"""The question runs (spec §4.10, §4.6): the run kinds a quiz question drives, not a source.

- ``source_proposal``: one metered model call with web search. Code keeps the candidates that
  survive (intel/source_choice.py), and each option's existing registry sources are listed first
  whatever the model does. Nothing is registered.
- ``source_validation`` and ``weight_suggestion`` join below in later tasks.

``pipeline.run()`` dispatches here after loading the vocabulary, and each kind returns its own
RunResult, because its status must say what the operator is waiting for. The pipeline's run
context, gate and stop semantics are shared rather than copied, which is why this module imports
the pipeline's internals and why the pipeline imports this module lazily.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from imageshield.intel.bounds import MAX_PROPOSED_SOURCES_PER_OPTION
from imageshield.intel.generation import prompt_registry
from imageshield.intel.pipeline import RunResult, RunStatus, _call_model, _CallCap, _Ctx, _Stop
from imageshield.intel.prompts import SOURCE_PROPOSAL_PROMPT_VERSION, source_proposal_request
from imageshield.intel.source_choice import (
    Candidate,
    QuestionRequest,
    clean_candidates,
    option_tags,
    prompt_source_question,
    source_identity,
)
from imageshield.intel.vocabulary import parse_vocabulary
from imageshield.search.urlhash import url_hash


async def run_question(ctx: _Ctx) -> RunResult:
    if ctx.run.kind == "source_proposal":
        return await _source_proposal(ctx)
    return RunResult("failed", ctx.outcome(), "kind_not_supported_yet")


# ── stage 1: source proposal ─────────────────────────────────────────────────


async def _source_proposal(ctx: _Ctx) -> RunResult:
    """spec §4.10 stage 1. A gate refusal, an unavailable model or a verdict leaves only the
    existing sources, and the run says why."""
    try:
        request = QuestionRequest.model_validate(ctx.run.request)
    except ValidationError:
        return RunResult("failed", ctx.outcome(), "request_unreadable")
    vocabulary = parse_vocabulary(ctx.vocabulary) if ctx.vocabulary is not None else None
    tags_by_option = {
        option: option_tags(
            option,
            question_key=request.question_key,
            request_tags=request.tags,
            vocabulary=vocabulary,
        )
        for option in request.options
    }
    all_tags = sorted({t for tags in tags_by_option.values() for t in tags})
    registry = await ctx.deps.questions.sources_with_tags(all_tags)
    existing = {
        option: [s for s in registry if set(s.tags) & set(tags)]
        for option, tags in tags_by_option.items()
    }
    proposed: dict[str, list[Candidate]] = {option: [] for option in request.options}
    status: RunStatus = "completed"
    error: str | None = None
    system, user = source_proposal_request(
        prompt_source_question(request, tags_by_option),
        registry_tags=prompt_registry(vocabulary, set(all_tags)) if vocabulary is not None else [],
        per_option=MAX_PROPOSED_SOURCES_PER_OPTION,
    )
    try:
        call = await _call_model(ctx, lambda: ctx.deps.model.propose_sources(system, user))
    except _Stop as stop:
        status, error = ("refused" if stop.gate else "failed"), stop.reason
    except _CallCap:  # unreachable while INTEL_MAX_CALLS_PER_RUN >= 1; kept honest anyway
        status, error = "failed", "run_call_cap"
    else:
        if call.output is None:
            ctx.counts[f"model_{call.outcome}"] += 1  # a verdict: consumed
            status, error = "failed", f"source_proposal_{call.outcome}"
        else:
            cleaned = clean_candidates(
                call.output,
                options=request.options,
                existing={
                    o: frozenset(source_identity(s) for s in ss) for o, ss in existing.items()
                },
                counts=ctx.counts,
            )
            hits = await ctx.deps.questions.known_hits(
                sorted(
                    {
                        str(url_hash(c.source_url))
                        for candidates in cleaned.values()
                        for c in candidates
                        if c.source_url is not None
                    }
                )
            )
            for option, candidates in cleaned.items():
                for candidate in candidates:
                    if candidate.source_url is not None and url_hash(candidate.source_url) in hits:
                        ctx.counts["candidate_dropped_known_hit_location"] += 1
                    elif len(proposed[option]) >= MAX_PROPOSED_SOURCES_PER_OPTION:
                        ctx.counts["candidate_dropped_over_cap"] += 1
                    else:
                        proposed[option].append(candidate)
    outcome: dict[str, Any] = {
        **ctx.outcome(),
        "prompt_version": SOURCE_PROPOSAL_PROMPT_VERSION,
        "options": [
            {
                "option": option,
                "tags": list(tags_by_option[option]),
                "existing": [str(s.source_id) for s in existing[option]],
                "proposed": [c.as_json() for c in proposed[option]],
            }
            for option in request.options
        ],
    }
    if status == "refused":
        outcome["refused_by"] = "gate"
    return RunResult(status, outcome, error)
