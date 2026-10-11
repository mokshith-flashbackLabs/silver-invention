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
  ordinary generation over what was read. *Amended 2026-10-03 (throughput):* the pages are read
  several at once (INTEL_SOURCE_READ_CONCURRENCY), and a saved search is not read inline at all:
  it is queued as its own discovery run (spec 2026-10-03-intel-throughput §5, §6).

``pipeline.run()`` dispatches here after loading the vocabulary, and each kind returns its own
RunResult, because its status must say what the operator is waiting for. The pipeline's run
context, gate and stop semantics are shared rather than copied, which is why this module imports
the pipeline's internals and why the pipeline imports this module lazily.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from imageshield.intel.bounds import (
    SUGGESTION_CONTEXT_DAYS,
    SUGGESTION_CONTEXT_MAX_SIGNALS,
    SUGGESTION_POOL_MAX,
    SUGGESTION_WAIT_MAX_SECONDS,
    SUGGESTION_WAIT_RETRY_SECONDS,
)
from imageshield.intel.fetch_client import FetchFailure
from imageshield.intel.generation import prompt_registry, prompt_signal
from imageshield.intel.models import Run, Source
from imageshield.intel.pii import contains_pii
from imageshield.intel.pipeline import (
    ResearchWait,
    RunResult,
    RunStatus,
    RunWait,
    _call_model,
    _CallCap,
    _Ctx,
    _generate,
    _Stop,
    read_each,
    read_source,
)
from imageshield.intel.prompts import (
    SOURCE_PROPOSAL_PROMPT_VERSION,
    SUGGEST_PROMPT_VERSION,
    RegistryTag,
    source_proposal_request,
    suggestion_request,
    validation_search_request,
)
from imageshield.intel.question_store import ResearchSourceRequest
from imageshield.intel.schemas import SourceProposalOutput
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
    OptionSuggestion,
    SuggestionRunRequest,
    attach_research,
    is_backed,
    keep_backed,
    plan_research,
    prompt_suggestion_question,
    select_context,
    slugs_named_by,
    validate_suggestion,
)
from imageshield.intel.text import normalise
from imageshield.intel.vocabulary import ScoringVocabulary, parse_vocabulary
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
    limits = ctx.deps.limits
    registry_tags = prompt_registry(vocabulary, set(all_tags)) if vocabulary is not None else []
    system, user = source_proposal_request(
        prompt_source_question(request, tags_by_option),
        registry_tags=registry_tags,
        per_option=limits.proposed_sources_per_answer,
        searches_per_option=limits.proposed_searches_per_answer,
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
            await _keep_candidates(
                ctx, call.output, options=request.options, existing=existing, proposed=proposed
            )
            await _ask_for_uncovered(
                ctx,
                request,
                tags_by_option,
                registry_tags=registry_tags,
                existing=existing,
                proposed=proposed,
            )
            uncovered = sum(1 for o in request.options if not proposed[o] and not existing[o])
            if uncovered:
                ctx.counts["source_proposal_uncovered"] += uncovered
            if not any(proposed[option] or existing[option] for option in request.options):
                # Nothing for any answer is not a result (spec 2026-10-08-intel-source-proposal-v2
                # §2): v1 "completed" a gender question with no search made and no source at all.
                ctx.counts["source_proposal_empty"] += 1
                status, error = "failed", "no_sources_found"
    outcome: dict[str, Any] = {
        **ctx.outcome(),
        "prompt_version": SOURCE_PROPOSAL_PROMPT_VERSION,
        "options": [
            {
                "option": option,
                "tags": list(tags_by_option[option]),
                "existing": [str(s.source_id) for s in existing[option]],
                "proposed": [c.as_json() for c in proposed[option]],
                # No source aimed at this answer, after the coverage asks (spec 2026-10-10 §2.1).
                "uncovered": not proposed[option] and not existing[option],
            }
            for option in request.options
        ],
    }
    if status == "refused":
        outcome["refused_by"] = "gate"
    return RunResult(status, outcome, error)


async def _keep_candidates(
    ctx: _Ctx,
    output: SourceProposalOutput,
    *,
    options: Sequence[str],
    existing: Mapping[str, Sequence[Source]],
    proposed: dict[str, list[Candidate]],
) -> None:
    """One proposal call's answer through the same cleaning, known-hit drop and per-answer caps
    (spec §4.10 stage 1), appended to ``proposed``. A candidate already proposed for the option
    counts as existing, so a coverage ask never repeats one."""
    limits = ctx.deps.limits
    cleaned = clean_candidates(
        output,
        options=options,
        existing={
            o: frozenset(source_identity(s) for s in existing[o])
            | frozenset(c.identity for c in proposed[o])
            for o in options
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
        searches = sum(1 for c in proposed[option] if c.kind == "search_query")
        for candidate in candidates:
            if candidate.source_url is not None and url_hash(candidate.source_url) in hits:
                ctx.counts["candidate_dropped_known_hit_location"] += 1
            elif len(proposed[option]) >= limits.proposed_sources_per_answer:
                ctx.counts["candidate_dropped_over_cap"] += 1
            elif (
                candidate.kind == "search_query"
                and searches >= limits.proposed_searches_per_answer
            ):
                ctx.counts["candidate_dropped_search_over_cap"] += 1
            else:
                searches += candidate.kind == "search_query"
                proposed[option].append(candidate)


async def _ask_for_uncovered(
    ctx: _Ctx,
    request: QuestionRequest,
    tags_by_option: Mapping[str, Sequence[str]],
    *,
    registry_tags: Sequence[RegistryTag],
    existing: Mapping[str, Sequence[Source]],
    proposed: dict[str, list[Candidate]],
) -> None:
    """spec 2026-10-10-intel-every-answer-researched §2.1: up to
    INTEL_SOURCE_PROPOSAL_COVERAGE_ASKS more proposal calls, each naming only the answers that
    still have no source aimed at them. A refusal, an unavailable model or a verdict ends the
    asking; what is already proposed stands."""
    limits = ctx.deps.limits
    for _ in range(limits.source_proposal_coverage_asks):
        uncovered = tuple(o for o in request.options if not proposed[o] and not existing[o])
        if not uncovered:
            return
        ctx.counts["source_proposal_coverage_asks"] += 1
        ask = request.model_copy(update={"options": uncovered})
        system, user = source_proposal_request(
            prompt_source_question(ask, tags_by_option),
            registry_tags=registry_tags,
            per_option=limits.proposed_sources_per_answer,
            searches_per_option=limits.proposed_searches_per_answer,
            coverage_ask=True,
        )
        try:
            more = await _call_model(ctx, partial(ctx.deps.model.propose_sources, system, user))
        except _Stop as stop:
            ctx.counts[f"source_proposal_coverage_{stop.reason}"] += 1
            return
        except _CallCap:
            ctx.counts["source_proposal_coverage_run_call_cap"] += 1
            return
        if more.output is None:
            ctx.counts[f"source_proposal_coverage_{more.outcome}"] += 1
            return
        await _keep_candidates(
            ctx, more.output, options=uncovered, existing=existing, proposed=proposed
        )


# ── stage 3: source validation ───────────────────────────────────────────────


async def _source_validation(ctx: _Ctx) -> RunResult:
    """spec §4.10 stage 3: one verdict per candidate, in the submitted order, each echoing its
    candidate. The run completes whenever it could judge them all, gate refusals included: a
    blocked candidate carries the reason, and the transient reasons mean "check again later"."""
    try:
        request = ValidationRequest.model_validate(ctx.run.request)
    except ValidationError:
        return RunResult("failed", ctx.outcome(), "request_unreadable")
    candidates = list(request.candidates)
    reasons: list[str | None] = [None] * len(candidates)
    refused = _Refused()

    async def judge(index: int) -> None:
        candidate = candidates[index]
        if candidate.kind == "search_query":
            reasons[index] = await _validate_search(ctx, candidate.query_text or "", refused)
        else:
            reasons[index] = await _validate_url(ctx, candidate.kind, candidate.source_url or "")

    # Several candidates at once (spec 2026-10-03-intel-throughput §5), every one judged: the
    # verdicts are kept by position, so the results stay in the submitted order.
    each = await read_each(ctx, range(len(candidates)), judge, while_calls_left=False)
    bug = each.first_bug()
    if bug is not None:
        raise bug
    results: list[dict[str, Any]] = []
    for candidate, reason in zip(candidates, reasons, strict=True):
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


@dataclass
class _Refused:
    """A gate refusal (or the run's call cap) met by one validation search, shared by all of
    them so no candidate that starts after it asks again (Review Focus 2). Candidates judged side
    by side (spec 2026-10-03-intel-throughput §5) that were already asking keep their own answer;
    the first refusal is the one kept."""

    reason: str | None = None


async def _validate_search(ctx: _Ctx, query: str, refused: _Refused) -> str | None:
    """One search_query candidate's reason (None: ready). The route refuses a person-shaped
    query before it is stored (final review I1); this check is the second line, for a run queued
    any other way, and still searches nothing."""
    if contains_pii(query):
        return "query_names_a_person"
    if refused.reason is not None:
        return refused.reason
    system, user = validation_search_request(query, searches=ctx.deps.limits.validation_searches)
    try:
        call = await _call_model(ctx, lambda: ctx.deps.model.search_once(system, user))
    except _Stop as stop:
        if stop.gate:
            refused.reason = refused.reason or stop.reason
            return stop.reason
        return "search_unavailable"
    except _CallCap:
        refused.reason = refused.reason or "run_call_cap"
        return "run_call_cap"
    if call.output is None:
        ctx.counts[f"model_{call.outcome}"] += 1
        return "no_results"
    urls: list[str] = []
    seen: set[str] = set()
    for found in call.output.candidates:
        if is_https(found.url) and (key := str(url_hash(found.url))) not in seen:
            seen.add(key)
            urls.append(found.url)
    for url in urls[: ctx.deps.limits.validation_result_pages]:
        reason = await _validate_url(ctx, "search_result", url)
        if reason is None:
            return None
        if reason == "fetcher_unavailable":
            return reason
    return "no_results"


# ── the weight suggestion (spec §4.10 stage 4's run, §4.6) ───────────────────


async def _weight_suggestion(ctx: _Ctx) -> RunResult:
    """Read, wait, suggest, generate. The run's status is the suggestion's: completed once one is
    written. A gate refusal while reading ends the run ``refused`` before any suggestion,
    because the same gate would refuse that too. Generation runs unless the gate refused.

    *Amended 2026-10-08 (spec 2026-10-08-intel-suggestion-waits-for-evidence §2):* a suggestion
    never answers from evidence it has not read yet. Whatever this pass could not read -- its
    saved searches, sources past the cap, sources an outage stopped -- is queued as that
    source's own read, and the run WAITS (``RunResult("waiting")``): it leaves the queue until
    none of those reads is open, or SUGGESTION_WAIT_MAX_SECONDS have passed. Coming back it
    reads nothing itself; an awaited source whose read ended unread is queued again (within the
    deadline, no sooner than SUGGESTION_WAIT_RETRY_SECONDS) and the run waits again."""
    try:
        request = SuggestionRunRequest.model_validate(ctx.run.request)
    except ValidationError:
        return RunResult("failed", ctx.outcome(), "request_unreadable")
    if ctx.run.awaiting_source_ids is not None:
        _carry_waiting_counts(ctx)
        wait = await _still_waiting(ctx)
        if wait is not None:
            return _waiting(ctx, wait)
    else:
        stop, awaited = await _read_new_sources(ctx, request)
        if stop is not None and stop.gate:
            return RunResult("refused", {**ctx.outcome(), "refused_by": "gate"}, stop.reason)
        if awaited:
            deadline = _now(ctx) + timedelta(seconds=SUGGESTION_WAIT_MAX_SECONDS)
            return _waiting(ctx, RunWait(awaiting=tuple(awaited), deadline=deadline))
    step = await _suggest(ctx, request)
    if step.wait is not None:
        return _waiting(ctx, step.wait)
    status, error = step.status, step.error
    if status != "refused":
        try:
            await _generate(ctx)
        except _Stop as generation:
            ctx.counts[f"proposals_deferred_{generation.reason}"] += 1
    outcome: dict[str, Any] = {**ctx.outcome(), "prompt_version": SUGGEST_PROMPT_VERSION}
    if status == "refused":
        outcome["refused_by"] = "gate"
    return RunResult(status, outcome, error)


def _now(ctx: _Ctx) -> datetime:
    now = ctx.deps.clock()
    return now if now.tzinfo is not None else now.replace(tzinfo=UTC)


def _waiting(ctx: _Ctx, wait: RunWait) -> RunResult:
    outcome: dict[str, Any] = {**ctx.outcome(), "prompt_version": SUGGEST_PROMPT_VERSION}
    return RunResult("waiting", outcome, None, wait)


def _carry_waiting_counts(ctx: _Ctx) -> None:
    """A run coming back from waiting starts a fresh context; its outcome so far is its earlier
    passes' counts, calls and cost, carried so the finished outcome tells the whole run."""
    for key, value in ctx.run.outcome.items():
        if key == "model_calls" and isinstance(value, int):
            ctx.model_calls += value
        elif key == "cost_usd" and isinstance(value, str):
            try:
                ctx.cost_usd += Decimal(value)
            except InvalidOperation:
                continue
        elif isinstance(value, int) and not isinstance(value, bool):
            ctx.counts[key] += value


async def _still_waiting(ctx: _Ctx) -> RunWait | None:
    """Coming back from waiting (spec 2026-10-08 §2.4). The queue let this run in because none of
    its awaited sources has a read open, or its deadline passed. Any of them whose read ended
    without the page being read (``_unread``: an outage, a gate refusal) is queued again, and the
    run waits again -- until the deadline, after which it answers from what it holds."""
    awaited = list(ctx.run.awaiting_source_ids or ())
    deadline = ctx.run.wait_deadline
    now = _now(ctx)
    sources = await ctx.deps.questions.sources_by_ids(awaited)
    unread = [source.source_id for source in sources if _unread(source)]
    if deadline is not None and now < deadline:
        if not unread:
            return None
        await ctx.deps.questions.queue_source_reads(unread, now=now)
        ctx.counts["suggestion_reads_requeued"] += len(unread)
        return RunWait(
            awaiting=tuple(awaited),
            deadline=deadline,
            not_before=now + timedelta(seconds=SUGGESTION_WAIT_RETRY_SECONDS),
        )
    ctx.counts["suggestion_wait_timed_out"] += 1
    if unread:
        ctx.counts["suggestion_sources_unread"] += len(unread)
    return None


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
    # A research search (spec 2026-10-10 §2.2.4) is disabled 'research_once' and read the same way.
    return no_evidence and (
        source.enabled or source.disabled_reason in ("unmapped", "research_once")
    )


async def _read_new_sources(
    ctx: _Ctx, request: SuggestionRunRequest
) -> tuple[_Stop | None, list[UUID]]:
    """Each newly registered source in the request's order, then each other named source no check
    has read yet (``_unread``), until the cap, a gate refusal or an outage ends reading. Every
    source left unread or part-read is made due, so its first scheduled check comes at once
    rather than a full interval later. Returns the stop.

    *Amended 2026-10-03 (throughput, spec 2026-10-03-intel-throughput §5, §6):* up to
    ``INTEL_SOURCE_READ_CONCURRENCY`` sources are read at once, started in that same order. The
    cap is exact (a call reserves its slot before it is sent); a source whose read fails never
    cancels another's, and once one stops or meets the cap, no further source starts.

    A SAVED SEARCH is not read here at all. Its read is a Claude research call of several
    minutes, which held the suggestion (and, one run at a time, the queue) for as long; it is
    queued as its own ``discovery`` run instead, which a free worker slot runs beside this one,
    and counted in ``sources_deferred`` (and ``search_sources_deferred``). The suggestion is
    written from the pages read here plus the evidence already collected; asking again once the
    search has been read includes what it found."""
    listed = await ctx.deps.questions.sources_by_ids(
        list(dict.fromkeys([*request.new_source_ids, *request.source_ids]))
    )
    by_id = {source.source_id: source for source in listed}
    to_read = list(request.new_source_ids)
    for named in request.source_ids:
        source = by_id.get(named)
        if named not in to_read and source is not None and _unread(source):
            to_read.append(named)
    sources: list[Source] = []
    searches: list[UUID] = []
    for source_id in to_read:
        source = by_id.get(source_id)
        if source is None:
            ctx.counts["source_missing"] += 1
        elif source.kind == "search_query":
            searches.append(source_id)
        else:
            sources.append(source)
    if searches:
        # Queued before this run reads anything, so the searches start as soon as a slot frees,
        # not after this run. A search with a run already open keeps that one.
        await ctx.deps.questions.queue_source_reads(searches, now=ctx.deps.clock())
        ctx.counts["search_sources_deferred"] += len(searches)
    each = await read_each(ctx, sources, lambda source: read_source(ctx, source))
    bug = each.first_bug()
    if bug is not None:
        raise bug
    stop = each.first_stop()
    if stop is not None:
        ctx.counts[f"stopped_{stop.reason}"] += 1
    deferred: list[UUID] = []
    for source, started, raised in zip(each.items, each.started, each.raised, strict=True):
        if not started or raised is not None or source.source_id in ctx.partly_read:
            # Never started (a stop or the cap came first), refused by the cap, stopped, or a
            # feed or search that left items for its next check.
            deferred.append(source.source_id)
        else:
            ctx.counts["sources_read"] += 1
    if deferred or searches:
        ctx.counts["sources_deferred"] += len(deferred) + len(searches)
    if stop is not None and stop.gate:
        # The run is refused, and the same gate would refuse every read queued now: the sources
        # are made due for their first scheduled check instead, as before.
        await ctx.deps.questions.mark_sources_due(deferred, now=ctx.deps.clock())
        return stop, []
    if deferred:
        # Each read this pass could not finish becomes that source's own run, which the
        # suggestion then waits for (spec 2026-10-08 §2.1).
        await ctx.deps.questions.queue_source_reads(deferred, now=ctx.deps.clock())
    return stop, [*searches, *deferred]


@dataclass(frozen=True)
class _SuggestStep:
    status: RunStatus
    error: str | None = None
    wait: RunWait | None = None


@dataclass(frozen=True)
class _ResearchState:
    """The run's research so far (migration 0053, spec 2026-10-10 §2.2): round 0 before any."""

    round: int
    source_ids: tuple[UUID, ...]
    kept: dict[str, Any] | None
    searched: dict[str, list[dict[str, Any]]]

    @classmethod
    def of(cls, run: Run) -> _ResearchState:
        kept = run.research_pass
        raw = kept.get("searches") if isinstance(kept, dict) else None
        searched = (
            {str(k): list(v) for k, v in raw.items() if isinstance(v, list)}
            if isinstance(raw, dict)
            else {}
        )
        return cls(run.research_round, tuple(run.research_source_ids or ()), kept, searched)


async def _suggest(ctx: _Ctx, request: SuggestionRunRequest) -> _SuggestStep:
    """Retrieval, ONE suggestion call outside the reading cap (like generation), validation in
    code, then either a research round or the write (spec 2026-10-10-intel-every-answer-researched
    §2.2). A later round that cannot finish -- the gate, the model, an unusable answer, no
    evidence -- writes the pass that started it."""
    questions = ctx.deps.questions
    limits = ctx.deps.limits
    if await questions.suggestion_written(ctx.run.run_id):
        ctx.counts["suggestion_already_written"] += 1  # a reclaimed run: never billed twice
        return _SuggestStep("completed")
    vocabulary = parse_vocabulary(ctx.vocabulary) if ctx.vocabulary is not None else None
    if vocabulary is None:
        return _SuggestStep("failed", "vocabulary_missing")
    now = _now(ctx)
    research = _ResearchState.of(ctx.run)
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
    # The research searches' evidence, as its own class taken first (final review C1): this
    # run's, and every earlier research search for the question (I3), whose pages a new search
    # would skip as recently fetched.
    research_ids = list(
        dict.fromkeys(
            [*research.source_ids, *await questions.research_sources_for(request.question_key)]
        )
    )
    candidates = await questions.suggestion_candidates(
        source_ids=request.source_ids,
        tags=sorted(option_tag_set | slugs_named_by(request.options, vocabulary)),
        since=now - timedelta(days=SUGGESTION_CONTEXT_DAYS),
        limit=SUGGESTION_POOL_MAX,
        research_source_ids=research_ids,
    )
    context = select_context(
        candidates, options=request.options, limit=SUGGESTION_CONTEXT_MAX_SIGNALS
    )
    ctx.counts["suggestion_evidence"] = len(context)
    if not context:
        if research.kept is not None:
            return await _write_kept(ctx, request, vocabulary, research)
        # Never an answer from no evidence at all (spec 2026-10-08 §2.5): no call, nothing
        # written, and the question's previous suggestion stays delivered.
        ctx.counts["suggestion_no_evidence"] += 1
        return _SuggestStep("failed", "no_evidence_found")
    relevant = option_tag_set | {t for s in context for t in s.tags} | set(vocabulary.mapped_tags)
    system, user = suggestion_request(
        prompt_suggestion_question(
            request, tags_by_option, vocabulary, searched=research.searched
        ),
        [prompt_signal(s) for s in context],
        registry_tags=prompt_registry(vocabulary, relevant),
        research_round=research.round + 1,
        max_rounds=limits.suggestion_research_rounds,
        searches_per_answer=limits.research_searches_per_answer,
    )
    try:
        call = await _call_model(
            ctx, lambda: ctx.deps.model.suggest_weights(system, user), capped=False
        )
    except _Stop as stop:
        if research.kept is not None:
            ctx.counts[f"research_ended_{stop.reason}"] += 1
            return await _write_kept(ctx, request, vocabulary, research)
        return _SuggestStep("refused" if stop.gate else "failed", stop.reason)
    if call.output is None:
        ctx.counts[f"suggestion_model_{call.outcome}"] += 1  # a verdict: nothing to deliver
        if research.kept is not None:
            return await _write_kept(ctx, request, vocabulary, research)
        return _SuggestStep("failed", f"suggestion_{call.outcome}")
    by_id = {s.signal_id: s for s in context}
    options = validate_suggestion(
        call.output,
        options=request.options,
        cap=request.cap,
        context=by_id,
        vocabulary=vocabulary,
        counts=ctx.counts,
    )
    kept_options = research.kept.get("options") if research.kept is not None else None
    kept_backed = research.kept.get("backed", {}) if research.kept is not None else {}
    options, backed = keep_backed(
        options,
        backed={o.option: is_backed(o, by_id) for o in options},
        kept=kept_options if isinstance(kept_options, list) else None,
        kept_backed={str(k): bool(v) for k, v in dict(kept_backed).items()},
        counts=ctx.counts,
    )
    unbacked = [o for o in options if not backed[o.option]]
    if unbacked and research.round < limits.suggestion_research_rounds:
        planned = plan_research(
            unbacked,
            searched=research.searched,
            per_answer=limits.research_searches_per_answer,
            counts=ctx.counts,
        )
        if planned:
            return await _start_round(
                ctx, request, research, tags_by_option, planned, options, backed, call.answered_by
            )
    final = attach_research(
        options,
        backed=backed,
        searched=research.searched,
        researching=limits.suggestion_research_rounds > 0,
    )
    return await _write(ctx, request, vocabulary, final, model_id=call.answered_by)


async def _start_round(
    ctx: _Ctx,
    request: SuggestionRunRequest,
    research: _ResearchState,
    tags_by_option: Mapping[str, Sequence[str]],
    planned: Mapping[str, Sequence[str]],
    options: Sequence[OptionSuggestion],
    backed: Mapping[str, bool],
    model_id: str,
) -> _SuggestStep:
    """Register each planned search as a read-once research source, queue its read, keep this
    pass, and wait (spec 2026-10-10 §2.2.4-§2.2.7)."""
    now = _now(ctx)
    ids = await ctx.deps.questions.register_research_sources(
        ctx.run.run_id,
        question_key=request.question_key,
        searches=[
            ResearchSourceRequest(option, query, tuple(tags_by_option.get(option, ())))
            for option, queries in planned.items()
            for query in queries
        ],
    )
    # Read only what is still unread (final review I4, 2026-10-11): a reused search already read
    # needs no second paid read, and one an operator switched off is left alone (``_unread``).
    # Its evidence is retrieved either way. With nothing to read, the run is claimable at once.
    registered = await ctx.deps.questions.sources_by_ids(ids)
    to_read = [s.source_id for s in registered if _unread(s)]
    await ctx.deps.questions.queue_source_reads(to_read, now=now)
    next_round = research.round + 1
    searched = {k: list(v) for k, v in research.searched.items()}
    for option, queries in planned.items():
        searched.setdefault(option, []).extend({"round": next_round, "query": q} for q in queries)
    ctx.counts["research_rounds"] += 1
    ctx.counts["research_searches"] += sum(len(q) for q in planned.values())
    kept = {
        "model_id": model_id,
        "options": [o.as_json() for o in options],
        "backed": dict(backed),
        "searches": searched,
    }
    return _SuggestStep(
        "waiting",
        wait=RunWait(
            awaiting=tuple(to_read),
            deadline=now + timedelta(seconds=SUGGESTION_WAIT_MAX_SECONDS),
            research=ResearchWait(
                round=next_round,
                source_ids=tuple(dict.fromkeys([*research.source_ids, *ids])),
                kept=kept,
                answers=len(planned),
            ),
        ),
    )


async def _write_kept(
    ctx: _Ctx,
    request: SuggestionRunRequest,
    vocabulary: ScoringVocabulary,
    research: _ResearchState,
) -> _SuggestStep:
    """The pass that started the round now ending (spec 2026-10-10 §2.2.8): its numbers, with
    every answer it left unbacked exhausted."""
    assert research.kept is not None
    options = [OptionSuggestion.from_json(o) for o in research.kept.get("options", [])]
    backed = {str(k): bool(v) for k, v in dict(research.kept.get("backed", {})).items()}
    final = attach_research(options, backed=backed, searched=research.searched, researching=True)
    model_id = str(research.kept.get("model_id", ""))
    return await _write(ctx, request, vocabulary, final, model_id=model_id)


async def _write(
    ctx: _Ctx,
    request: SuggestionRunRequest,
    vocabulary: ScoringVocabulary,
    options: Sequence[OptionSuggestion],
    *,
    model_id: str,
) -> _SuggestStep:
    exhausted = sum(1 for o in options if o.research is not None and o.research.exhausted)
    if exhausted:
        ctx.counts["research_exhausted_answers"] += exhausted
    written = await ctx.deps.questions.write_suggestion(
        ctx.run.run_id,
        question_key=request.question_key,
        options=options,
        against_scoring_version=vocabulary.scoring_version,
        against_release_no=vocabulary.release_no,
        model_id=model_id,
        prompt_version=SUGGEST_PROMPT_VERSION,
    )
    if written is None:
        ctx.counts["suggestion_already_written"] += 1
    else:
        ctx.counts["suggestions_superseded"] += len(written.superseded)
    return _SuggestStep("completed")
