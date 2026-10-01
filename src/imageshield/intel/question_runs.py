"""The question runs (spec §4.10, §4.6): the run kinds a quiz question drives, not a source.

- ``source_proposal``: one metered model call with web search. Code keeps the candidates that
  survive (intel/source_choice.py), and each option's existing registry sources are listed first
  whatever the model does. Nothing is registered.
- ``source_validation``: code alone gives every candidate a verdict. A URL candidate makes no
  model request; a search_query candidate costs one metered web search, whose pages code then
  judges exactly like a URL.
- ``weight_suggestion``: the immediate read of the sources stage 4 registered (and of any
  named source no check has read yet, so a retried press reads what a refused one left), under
  INTEL_MAX_CALLS_PER_SUGGESTION_RUN and through ``read_source`` (the scheduled check's own code),
  then retrieval and ONE suggestion call (intel/suggestion.py decides what is kept), then the
  ordinary generation over what was read.

``pipeline.run()`` dispatches here after loading the vocabulary, and each kind returns its own
RunResult, because its status must say what the operator is waiting for. The pipeline's run
context, gate and stop semantics are shared rather than copied, which is why this module imports
the pipeline's internals and why the pipeline imports this module lazily.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from imageshield.intel.bounds import (
    MAX_PROPOSED_SOURCES_PER_OPTION,
    MAX_SEARCH_RESULTS_CHECKED,
    SUGGESTION_CONTEXT_DAYS,
    SUGGESTION_CONTEXT_MAX_SIGNALS,
    SUGGESTION_POOL_MAX,
)
from imageshield.intel.fetch_client import FetchFailure
from imageshield.intel.generation import prompt_registry, prompt_signal
from imageshield.intel.models import Source
from imageshield.intel.pii import contains_pii
from imageshield.intel.pipeline import (
    RunResult,
    RunStatus,
    _call_model,
    _CallCap,
    _Ctx,
    _generate,
    _Stop,
    read_source,
)
from imageshield.intel.prompts import (
    SOURCE_PROPOSAL_PROMPT_VERSION,
    SUGGEST_PROMPT_VERSION,
    source_proposal_request,
    suggestion_request,
    validation_search_request,
)
from imageshield.intel.source_choice import (
    FETCH_REASONS,
    Candidate,
    QuestionRequest,
    ValidationRequest,
    clean_candidates,
    is_https,
    option_tags,
    prompt_source_question,
    source_identity,
    url_verdict,
)
from imageshield.intel.suggestion import (
    SuggestionRunRequest,
    prompt_suggestion_question,
    select_context,
    slugs_named_by,
    validate_suggestion,
)
from imageshield.intel.text import normalise
from imageshield.intel.vocabulary import parse_vocabulary
from imageshield.search.urlhash import canonicalise, url_hash


async def run_question(ctx: _Ctx) -> RunResult:
    if ctx.run.kind == "source_proposal":
        return await _source_proposal(ctx)
    if ctx.run.kind == "source_validation":
        return await _source_validation(ctx)
    if ctx.run.kind == "weight_suggestion":
        return await _weight_suggestion(ctx)
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


# ── stage 3: source validation ───────────────────────────────────────────────


async def _source_validation(ctx: _Ctx) -> RunResult:
    """spec §4.10 stage 3: one verdict per candidate, in the submitted order, each echoing its
    candidate. The run completes whenever it could judge them all, gate refusals included: a
    blocked candidate carries the reason, and the transient reasons mean "check again later"."""
    try:
        request = ValidationRequest.model_validate(ctx.run.request)
    except ValidationError:
        return RunResult("failed", ctx.outcome(), "request_unreadable")
    results: list[dict[str, Any]] = []
    refused: str | None = None
    for candidate in request.candidates:
        if candidate.kind == "search_query":
            reason, refused = await _validate_search(ctx, candidate.query_text or "", refused)
        else:
            reason = await _validate_url(ctx, candidate.kind, candidate.source_url or "")
        ctx.counts["candidate_ready" if reason is None else f"candidate_blocked_{reason}"] += 1
        results.append(
            {
                "candidate": candidate.as_json(),
                "status": "ready" if reason is None else "blocked",
                "reason": reason,
            }
        )
    return RunResult("completed", {**ctx.outcome(), "results": results})


async def _validate_url(ctx: _Ctx, kind: str, url: str) -> str | None:
    """One URL through the stage-3 checks; None is ready. Never a model request. The canonical
    URL is what stage 4 registers, so it is what is checked."""
    canonical = canonicalise(url)
    if not is_https(canonical):
        return "not_https"
    if await ctx.deps.store.is_known_hit(url_hash(canonical)):
        return "known_hit_location"  # before any fetch (§6.1)
    fetched = await ctx.deps.fetcher.fetch_text(canonical, respect_robots=True)
    if isinstance(fetched, FetchFailure):
        return FETCH_REASONS.get(fetched.code, "unreachable")
    ctx.counts["documents_fetched"] += 1
    if not is_https(fetched.final_url):
        return "not_https"
    moved = url_hash(fetched.final_url) != url_hash(canonical)
    if moved and await ctx.deps.store.is_known_hit(url_hash(fetched.final_url)):
        return "known_hit_location"
    return url_verdict(kind, normalise(fetched.text), fetched.items)


async def _validate_search(
    ctx: _Ctx, query: str, refused: str | None
) -> tuple[str | None, str | None]:
    """One search_query candidate, as ``(reason, refused)``. ``refused`` carries a gate refusal
    (or the run's call cap) forward, so no later candidate asks again (Review Focus 2). The
    route refuses a person-shaped query before it is stored (final review I1); this check is
    the second line, for a run queued any other way, and still searches nothing."""
    if contains_pii(query):
        return "query_names_a_person", refused
    if refused is not None:
        return refused, refused
    system, user = validation_search_request(query)
    try:
        call = await _call_model(ctx, lambda: ctx.deps.model.search_once(system, user))
    except _Stop as stop:
        if stop.gate:
            return stop.reason, stop.reason
        return "search_unavailable", refused
    except _CallCap:
        return "run_call_cap", "run_call_cap"
    if call.output is None:
        ctx.counts[f"model_{call.outcome}"] += 1
        return "no_results", refused
    urls: list[str] = []
    seen: set[str] = set()
    for found in call.output.candidates:
        if is_https(found.url) and (key := str(url_hash(found.url))) not in seen:
            seen.add(key)
            urls.append(found.url)
    for url in urls[:MAX_SEARCH_RESULTS_CHECKED]:
        reason = await _validate_url(ctx, "search_result", url)
        if reason is None:
            return None, refused
        if reason == "fetcher_unavailable":
            return reason, refused
    return "no_results", refused


# ── the weight suggestion (spec §4.10 stage 4's run, §4.6) ───────────────────


async def _weight_suggestion(ctx: _Ctx) -> RunResult:
    """Read, suggest, generate. The run's status is the suggestion's: completed once one is
    written. A gate refusal while reading ends the run ``refused`` before any suggestion,
    because the same gate would refuse that too; an outage while reading lets the suggestion
    go ahead with what was read. Generation runs unless the gate refused."""
    try:
        request = SuggestionRunRequest.model_validate(ctx.run.request)
    except ValidationError:
        return RunResult("failed", ctx.outcome(), "request_unreadable")
    stop = await _read_new_sources(ctx, request)
    if stop is not None and stop.gate:
        return RunResult("refused", {**ctx.outcome(), "refused_by": "gate"}, stop.reason)
    status, error = await _suggest(ctx, request)
    if status != "refused":
        try:
            await _generate(ctx)
        except _Stop as generation:
            ctx.counts[f"proposals_deferred_{generation.reason}"] += 1
    outcome: dict[str, Any] = {**ctx.outcome(), "prompt_version": SUGGEST_PROMPT_VERSION}
    if status == "refused":
        outcome["refused_by"] = "gate"
    return RunResult(status, outcome, error)


def _unread(source: Source) -> bool:
    """A named source this run reads although it registered nothing, because no check of it has
    produced evidence yet (final review M6): never checked, or its last check stopped before
    the page was read (``deferred_<reason>``: a gate refusal or the model down). An earlier
    press may have registered it and then been refused, and a source paused for unmapped tags
    (a draft option) gets no scheduled check until the draft publishes, so without this its
    evidence would never reach a suggestion. One an operator disabled is left alone."""
    no_evidence = source.last_checked_at is None or (source.last_run_status or "").startswith(
        "deferred_"
    )
    return no_evidence and (source.enabled or source.disabled_reason == "unmapped")


async def _read_new_sources(ctx: _Ctx, request: SuggestionRunRequest) -> _Stop | None:
    """Each newly registered source in the request's order, then each other named source no check
    has read yet (``_unread``), until the cap, a gate refusal or an outage ends reading. Every
    source left unread or part-read is made due, so its first scheduled check comes at once
    rather than a full interval later. Returns the stop."""
    listed = await ctx.deps.questions.sources_by_ids(
        list(dict.fromkeys([*request.new_source_ids, *request.source_ids]))
    )
    by_id = {source.source_id: source for source in listed}
    to_read = list(request.new_source_ids)
    for named in request.source_ids:
        source = by_id.get(named)
        if named not in to_read and source is not None and _unread(source):
            to_read.append(named)
    deferred: list[UUID] = []
    stop: _Stop | None = None
    for source_id in to_read:
        source = by_id.get(source_id)
        if source is None:
            ctx.counts["source_missing"] += 1
            continue
        if stop is not None or not ctx.calls_left():
            deferred.append(source_id)
            continue
        before = ctx.counts["call_cap_deferred"]
        try:
            await read_source(ctx, source)
        except _CallCap:
            deferred.append(source_id)
            continue
        except _Stop as reading:
            ctx.counts[f"stopped_{reading.reason}"] += 1
            stop = reading
            deferred.append(source_id)
            continue
        if ctx.counts["call_cap_deferred"] > before:
            deferred.append(source_id)  # a feed or a search left items for its next check
        else:
            ctx.counts["sources_read"] += 1
    if deferred:
        await ctx.deps.questions.mark_sources_due(deferred, now=ctx.deps.clock())
        ctx.counts["sources_deferred"] += len(deferred)
    return stop


async def _suggest(ctx: _Ctx, request: SuggestionRunRequest) -> tuple[RunStatus, str | None]:
    """Retrieval, ONE suggestion call outside the reading cap (like generation), validation in
    code, and the write."""
    questions = ctx.deps.questions
    if await questions.suggestion_written(ctx.run.run_id):
        ctx.counts["suggestion_already_written"] += 1  # a reclaimed run: never billed twice
        return "completed", None
    vocabulary = parse_vocabulary(ctx.vocabulary) if ctx.vocabulary is not None else None
    if vocabulary is None:
        return "failed", "vocabulary_missing"
    now = ctx.deps.clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    tags_by_option = {
        option: option_tags(
            option,
            question_key=request.question_key,
            request_tags=request.tags,
            vocabulary=vocabulary,
        )
        for option in request.options
    }
    option_tag_set = {t for tags in tags_by_option.values() for t in tags}
    candidates = await questions.suggestion_candidates(
        source_ids=request.source_ids,
        tags=sorted(option_tag_set | slugs_named_by(request.options, vocabulary)),
        since=now - timedelta(days=SUGGESTION_CONTEXT_DAYS),
        limit=SUGGESTION_POOL_MAX,
    )
    context = select_context(
        candidates, options=request.options, limit=SUGGESTION_CONTEXT_MAX_SIGNALS
    )
    ctx.counts["suggestion_evidence"] = len(context)
    relevant = option_tag_set | {t for s in context for t in s.tags} | set(vocabulary.mapped_tags)
    system, user = suggestion_request(
        prompt_suggestion_question(request, tags_by_option, vocabulary),
        [prompt_signal(s) for s in context],
        registry_tags=prompt_registry(vocabulary, relevant),
    )
    try:
        call = await _call_model(
            ctx, lambda: ctx.deps.model.suggest_weights(system, user), capped=False
        )
    except _Stop as stop:
        return ("refused" if stop.gate else "failed"), stop.reason
    if call.output is None:
        ctx.counts[f"suggestion_model_{call.outcome}"] += 1  # a verdict: nothing to deliver
        return "failed", f"suggestion_{call.outcome}"
    options = validate_suggestion(
        call.output,
        options=request.options,
        cap=request.cap,
        context={s.signal_id: s for s in context},
        vocabulary=vocabulary,
        counts=ctx.counts,
    )
    written = await questions.write_suggestion(
        ctx.run.run_id,
        question_key=request.question_key,
        options=options,
        against_scoring_version=vocabulary.scoring_version,
        against_release_no=vocabulary.release_no,
        model_id=call.answered_by,
        prompt_version=SUGGEST_PROMPT_VERSION,
    )
    if written is None:
        ctx.counts["suggestion_already_written"] += 1
    else:
        ctx.counts["suggestions_superseded"] += len(written.superseded)
    return "completed", None
