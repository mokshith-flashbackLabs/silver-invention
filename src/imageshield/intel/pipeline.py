"""The intel pipeline (spec §4.3): ``source_check``, ``discovery`` and ``adhoc_url``,
then extraction and local verification.

A UNIT is one policy diff, one feed item or one whole document. Deterministic results
CONSUME it, in the single ``record_unit`` transaction that writes its document row,
signals, excerpts, snapshot and hash: signals kept, a model refusal, a ``max_tokens``
stop, unparseable output, every quote dropped. Transient results leave it UNCONSUMED
for the next check: a gate skip, ``budget_unset``, a timeout / error / rate limit, a
fetch failure, the per-run call cap. A run the gate stops ends ``refused`` with
``refused_by = 'gate'``; one stopped by an unavailable model or fetcher ends
``failed``. Either way it keeps every unit it had already consumed.

What the model reads arrived through the fetcher (INVARIANTS #11) and is PII-masked
first (§6.3) -- nothing is lost by that: a quote holding a phone- or email-shaped run
would be dropped by verification anyway. Every quote is then verified against the
UNMASKED normalised text we fetched (#49), so a model that "corrects" a garbled
character or paraphrases produces nothing. A known hit location is never fetched,
and a page that redirects onto one is never read (§6.1).

GENERATION (step 2). A run that wrote new signals makes ONE more call, INTEL_PROPOSAL_MODEL,
through the same gate. That call is outside INTEL_MAX_CALLS_PER_RUN, which bounds reading. Its
output is validated in code (intel/generation.py) and written with proposals_written_at in one
transaction (intel/proposal_store.py), so a reclaimed run never generates twice. A verdict
(refusal, max_tokens, unparseable) is consumed like any other; a gate skip or an unavailable
model leaves generation undone and stops the run.

EVENTS AND REGENERATION (step 3). The generation call also sees the pending event proposals and
live threat events whose tags overlap the run's evidence, and may propose threat_events and
attach new evidence to a pending one (intel/generation.py decides what is written). A
``gap_regenerate`` run reads nothing: its evidence is the signals the reconcile named when it
closed a gap (spec §4.9), and it proposes events only. A gate refusal leaves it unwritten, and
the reconcile's gap pass queues it again later.

PROTECTIONS (step 4). The generation call also sees the live protection credits on the evidence's
tags (and every live global one), and may propose protection_events, never global ones. A
``renewal_check`` run is different in kind: it makes no model call (spec §4.8).

QUESTIONS (steps 5 and 6). ``source_proposal``, ``source_validation`` and ``weight_suggestion`` are
driven by a quiz question rather than a source; ``intel/question_runs.py`` executes them and
returns its own result (spec §4.10).
"""

from __future__ import annotations

import asyncio
import difflib
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

import structlog
from pydantic import BaseModel, ValidationError

from imageshield.intel.bounds import (
    COVERAGE_GAP_POOL_MAX,
    COVERAGE_GAP_WINDOW_DAYS,
    DISCOVERY_DEDUP_DAYS,
    FEED_MAX_ITEM_AGE_DAYS,
    FEED_MAX_ITEMS_PER_RUN,
    MAX_PROMPT_TAGS,
    MIN_POLICY_TEXT_CHARS,
    PROPOSAL_CONTEXT_DAYS,
    PROPOSAL_CONTEXT_MAX_EVENTS,
    PROPOSAL_CONTEXT_MAX_NEW_SIGNALS,
    PROPOSAL_CONTEXT_MAX_SIGNALS,
)
from imageshield.intel.evidence_store import (
    DocumentRecord,
    EvidenceStore,
    SignalRecord,
    SnapshotRecord,
)
from imageshield.intel.fetch_client import FETCHER_SIDE_CODES, FetchFailure, TextFetch, TextFetcher
from imageshield.intel.generation import (
    GeneratedBatch,
    prompt_live_event,
    prompt_live_protection,
    prompt_pending_event,
    prompt_quiz,
    prompt_registry,
    prompt_signal,
    validate_proposals,
)
from imageshield.intel.metering import metered
from imageshield.intel.model import IntelModel, ModelCall
from imageshield.intel.models import Run, Source, Vocabulary
from imageshield.intel.pii import mask
from imageshield.intel.prompts import (
    EXTRACT_PROMPT_VERSION,
    PROPOSE_PROMPT_VERSION,
    RegistryTag,
    discovery_request,
    extraction_request,
    proposal_request,
)
from imageshield.intel.proposal_models import ContextSignal, GapRegenerateRequest, RenewalRequest
from imageshield.intel.proposal_store import ProposalStore
from imageshield.intel.protection_store import (
    RENEWAL_EVIDENCE_GONE,
    RENEWAL_EVIDENCE_UNREACHABLE,
    RENEWAL_NOT_DUE,
    RENEWAL_PROPOSED,
    ProtectionStore,
)
from imageshield.intel.publisher import publisher_domain
from imageshield.intel.question_store import QuestionStore
from imageshield.intel.recency import choose_published, parse_page_age, stated_date
from imageshield.intel.reconcile import Reconciler
from imageshield.intel.renewal import RenewalPage, plan_renewal
from imageshield.intel.schemas import ExtractedSignal
from imageshield.intel.store import IntelStore
from imageshield.intel.tags import TagRegistry, is_well_formed
from imageshield.intel.text import content_sha256, normalise
from imageshield.intel.verify import VerifiedQuote, verify_quote
from imageshield.intel.vocabulary import parse_vocabulary
from imageshield.providers.store import ProviderControlStore
from imageshield.search.urlhash import canonicalise, url_hash

log = structlog.get_logger("imageshield.intel.pipeline")

T = TypeVar("T", bound=BaseModel)
RunStatus = Literal["completed", "refused", "failed"]
Trust = Literal["listed", "web"]

# intel_signals.summary's CHECK (0038). The schema caps the model's summary at the
# same length, but masking can LENGTHEN it («masked» is longer than a short email),
# so the cap is applied again after masking -- a DB CHECK must never crash a run.
_SUMMARY_MAX_CHARS = 500
# The policy diff (spec §4.3 step 4): sentences of context kept around each change,
# and the sentence count past which diffing is skipped for the whole (bounded) text.
_DIFF_CONTEXT_SENTENCES = 2
_DIFF_MAX_SENTENCES = 10_000
_HUNK_SEPARATOR = "\n[...]\n"
_SENTENCE_END = re.compile(r"(?<=[.!?]) ")
# The run kinds a quiz question drives rather than a source (spec §4.10): intel/question_runs.py
# executes them. See run().
_QUESTION_RUN_KINDS = frozenset({"source_proposal", "source_validation", "weight_suggestion"})


@dataclass
class PipelineDeps:
    """Everything a run touches. Mutable on purpose: the worker's tests move
    ``clock`` past a lease. ``max_calls_per_run`` and ``max_document_chars`` are
    ``INTEL_MAX_CALLS_PER_RUN`` / ``INTEL_MAX_DOCUMENT_CHARS`` and have NO default
    here -- a second default beside IntelConfig's would be a second source of truth.
    ``reconciler`` is not part of a run: the worker's tick calls it before claiming
    (spec §4.9). ``questions`` is the question runs' store (step 5, ``intel/question_store.py``).
    ``max_calls_per_suggestion_run`` is ``INTEL_MAX_CALLS_PER_SUGGESTION_RUN``, a weight
    suggestion's reading cap (spec §4.10), with no default for the same reason.
    ``source_read_concurrency`` is ``INTEL_SOURCE_READ_CONCURRENCY`` (spec
    2026-10-03-intel-throughput §5): how many sources or pages one run reads at once."""

    store: IntelStore
    evidence: EvidenceStore
    fetcher: TextFetcher
    model: IntelModel
    control: ProviderControlStore
    proposals: ProposalStore
    reconciler: Reconciler
    protections: ProtectionStore  # credits and their renewal (step 4, intel/protection_store.py)
    clock: Callable[[], datetime]
    max_calls_per_run: int
    max_document_chars: int
    questions: QuestionStore
    max_calls_per_suggestion_run: int
    source_read_concurrency: int


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    # Counts; a question run adds its results (spec §4.10: "their results live in the run's
    # outcome").
    outcome: dict[str, Any]
    error_code: str | None = None


class _Stop(Exception):
    """The run stops here. Units consumed so far stay consumed; this one and the rest
    are left for the next check. ``gate`` separates a provider-gate refusal (the run
    ends ``refused``) from an unavailable model or fetcher (``failed``)."""

    def __init__(self, reason: str, *, gate: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.gate = gate


class _CallCap(Exception):
    """``INTEL_MAX_CALLS_PER_RUN`` reached: stop taking units. Not a failure."""


@dataclass
class _Ctx:
    run: Run
    deps: PipelineDeps
    vocabulary: Vocabulary | None
    registry: TagRegistry | None
    counts: Counter[str] = field(default_factory=Counter)
    model_calls: int = 0
    cost_usd: Decimal = Decimal("0")
    regenerate: GapRegenerateRequest | None = None
    # Sources a feed or a saved search read only in part (the call cap left items for its next
    # check). Keyed by source, so reads running side by side cannot mistake one another's.
    partly_read: set[UUID] = field(default_factory=set)

    def calls_left(self) -> bool:
        """The reading cap: a weight suggestion's first read of the sources it registered has its
        own, because it is legitimately larger than a weekly check (spec §4.10)."""
        if self.run.kind == "weight_suggestion":
            return self.model_calls < self.deps.max_calls_per_suggestion_run
        return self.model_calls < self.deps.max_calls_per_run

    def outcome(self) -> dict[str, int | str]:
        return {**self.counts, "model_calls": self.model_calls, "cost_usd": str(self.cost_usd)}

    def registry_tags(self, hints: Sequence[str], text: str) -> list[RegistryTag]:
        """The loaded registry for the prompt, retired tags omitted. Past
        ``MAX_PROMPT_TAGS`` it narrows to the source's hints plus tags whose slug or
        label appears in the text (spec §4.3)."""
        if self.vocabulary is None:
            return []
        tags = [
            RegistryTag(
                slug=str(t["slug"]),
                label=str(t.get("label", t["slug"])),
                description=str(t.get("description", "")),
            )
            for t in self.vocabulary.document.get("tags", [])
            if isinstance(t, dict) and "slug" in t and not t.get("retired")
        ]
        if len(tags) <= MAX_PROMPT_TAGS:
            return tags
        lowered = text.lower()
        return [
            t
            for t in tags
            if t["slug"] in hints or t["slug"] in lowered or t["label"].lower() in lowered
        ][:MAX_PROMPT_TAGS]


async def run(claimed: Run, deps: PipelineDeps) -> RunResult:
    """Execute one claimed run. Never raises for anything a page, a feed or the
    model can do; the caller finishes the run with the result."""
    vocabulary = await deps.store.load_vocabulary()
    if vocabulary is not None:
        await deps.store.set_run_vocabulary(
            claimed.run_id, release_no=vocabulary.release_no, map_version=vocabulary.map_version
        )
    ctx = _Ctx(
        run=claimed,
        deps=deps,
        vocabulary=vocabulary,
        registry=vocabulary.registry() if vocabulary is not None else None,
    )
    if claimed.kind in _QUESTION_RUN_KINDS:
        # A question run reads no source by itself and returns its own result (spec §4.10).
        # Imported here: question_runs imports this module.
        from imageshield.intel.question_runs import run_question

        return await run_question(ctx)
    stop: _Stop | None = None
    try:
        if claimed.kind == "source_check":
            await _source_check(ctx)
        elif claimed.kind == "discovery":
            await _discovery(ctx)
        elif claimed.kind == "adhoc_url":
            await _adhoc(ctx)
        elif claimed.kind == "gap_regenerate":
            # Nothing to read: the evidence is the signals the reconcile named (spec §4.9), and
            # the generation step below is the whole run.
            try:
                ctx.regenerate = GapRegenerateRequest.model_validate(claimed.request)
            except ValidationError:
                return RunResult("failed", ctx.outcome(), "request_unreadable")
        elif claimed.kind == "renewal_check":
            # No model call at all (spec §4.8): the run returns its own result and never
            # reaches generation below.
            return await _renewal_check(ctx)
        else:
            # Unreachable for every kind intel_runs' CHECK admits: the three question kinds
            # (source_proposal, source_validation, weight_suggestion) returned above, and
            # gap_regenerate (step 3) and renewal_check (step 4) are handled here. A defensive
            # refusal for a kind this build does not know, never a silent no-op.
            return RunResult("failed", ctx.outcome(), "kind_not_supported_yet")
    except _CallCap:
        ctx.counts["stopped_call_cap"] += 1
    except _Stop as reading:
        ctx.counts[f"stopped_{reading.reason}"] += 1
        stop = reading
    # Generation runs unless the GATE stopped reading: the same gate just refused, and a
    # second call would only record a second skip.
    if stop is None or not stop.gate:
        try:
            await _generate(ctx)
        except _Stop as generation:
            ctx.counts[f"proposals_deferred_{generation.reason}"] += 1
            stop = stop or generation
    if stop is not None:
        if stop.gate:
            return RunResult("refused", {**ctx.outcome(), "refused_by": "gate"}, stop.reason)
        return RunResult("failed", ctx.outcome(), stop.reason)
    return RunResult("completed", ctx.outcome())


# ── run kinds ──────────────────────────────────────────────────────────────────


async def _source_check(ctx: _Ctx) -> None:
    assert ctx.run.source_id is not None  # intel_runs CHECK: source_check has a source
    source = await ctx.deps.store.get_source(ctx.run.source_id)
    if source is None or source.source_url is None:
        ctx.counts["source_missing"] += 1
        return
    await _check_listed(ctx, source, source.source_url)


async def _check_listed(ctx: _Ctx, source: Source, url: str) -> None:
    """One check of a listed source, recorded on the source. ``read_source`` reuses it for a
    weight suggestion's immediate read."""
    # Before the fetch, not only after it: a listed URL that IS a known hit location
    # is never requested at all (spec §4.3, §6.1).
    if await _known_hit(ctx, url):
        await ctx.deps.evidence.record_check(source.source_id, ok=True, status="known_hit_location")
        return
    fetched = await _fetch(ctx, url)  # a fetcher-side failure raises _Stop
    if isinstance(fetched, FetchFailure):
        await ctx.deps.evidence.record_check(source.source_id, ok=False, status=fetched.code)
        return
    try:
        status = await _consume_source_fetch(ctx, source, fetched)
    except _Stop as stop:
        # The source answered; only the unit waits (a gate skip, the model down).
        await ctx.deps.evidence.record_check(
            source.source_id, ok=True, status=f"deferred_{stop.reason}"
        )
        raise
    await ctx.deps.evidence.record_check(source.source_id, ok=True, status=status)


async def _consume_source_fetch(ctx: _Ctx, source: Source, fetched: TextFetch) -> str:
    if await _known_hit_after_redirect(ctx, source.source_url or "", fetched.final_url):
        return "known_hit_location"
    if source.kind == "feed":
        await _feed(ctx, source, fetched)
        return "checked"

    full = normalise(fetched.text)
    if source.kind == "policy_page" and len(full) < MIN_POLICY_TEXT_CHARS:
        # A single-page-app shell: disabled in one transaction with its audit row,
        # no snapshot and no model call (spec §4.3 step 2).
        await ctx.deps.evidence.disable_source(source.source_id, reason="too_short")
        ctx.counts["disabled_too_short"] += 1
        return "too_short"
    text, truncated = _bounded(ctx, full, fetched.truncated)
    masked, _ = mask(text)

    snapshot: SnapshotRecord | None = None
    source_hash: tuple[UUID, str] | None = None
    if source.kind == "policy_page":
        # The snapshot is masked, so its hash is of the masked text: a change only
        # inside a masked run is no change at all, and the diff is masked-vs-masked.
        digest = content_sha256(masked)
        previous = await ctx.deps.evidence.snapshot_for(source.source_id)
        if previous is not None and previous.content_sha256 == digest:
            ctx.counts["unchanged"] += 1
            return "unchanged"
        model_text = masked
        if previous is not None:
            model_text = await asyncio.to_thread(
                changed_hunks,
                previous.snapshot_text,
                masked,
                max_chars=ctx.deps.max_document_chars,
            )
            ctx.counts["policy_diff"] += 1
        snapshot = SnapshotRecord(
            source_id=source.source_id,
            content_sha256=digest,
            snapshot_text=masked,
            content_type=fetched.content_type,
            truncated=truncated,
        )
    else:
        digest = content_sha256(text)
        if source.last_content_sha256 == digest:
            ctx.counts["unchanged"] += 1
            return "unchanged"
        model_text = masked
        source_hash = (source.source_id, digest)

    await _extract_unit(
        ctx,
        requested_url=source.source_url or fetched.final_url,
        fetched=fetched,
        text=text,
        model_text=model_text,
        truncated=truncated,
        source_id=source.source_id,
        trust="listed",
        tag_hints=source.tags,
        source_kind=source.kind,
        snapshot=snapshot,
        source_hash=source_hash,
    )
    return "checked"


@dataclass(frozen=True)
class _FeedItem:
    index: int
    link: str
    link_hash: str
    title: str
    published: datetime | None


async def _feed(ctx: _Ctx, source: Source, fetched: TextFetch) -> None:
    """Up to ``FEED_MAX_ITEMS_PER_RUN`` unseen items, newest first; the rest are
    ``feed_backlog`` and taken on the next run, whatever the listing's own hash."""
    now = ctx.deps.clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    horizon = now - timedelta(days=FEED_MAX_ITEM_AGE_DAYS)
    if fetched.items is None:
        ctx.counts["feed_no_items"] += 1  # the URL answered, but not with a feed

    candidates: list[_FeedItem] = []
    listed: set[str] = set()
    for index, item in enumerate(fetched.items or []):
        link = item.get("link")
        if not isinstance(link, str) or not link.strip():
            ctx.counts["feed_item_no_link"] += 1
            continue
        if not _is_https(link):
            ctx.counts["not_https"] += 1
            continue
        published = _published(item.get("published"))
        try:
            if published is not None and published < horizon:
                ctx.counts["too_old"] += 1  # skipped without a record: free to re-skip
                continue
        except TypeError:  # belt and braces: _published never returns a naive datetime
            published = None
        link_hash = url_hash(link)
        if link_hash in listed:
            ctx.counts["feed_item_duplicate"] += 1
            continue
        listed.add(link_hash)
        title = item.get("title")
        candidates.append(
            _FeedItem(index, link, link_hash, title if isinstance(title, str) else "", published)
        )

    hashes = [c.link_hash for c in candidates]
    seen = await ctx.deps.evidence.seen_url_hashes(source.source_id, hashes)
    recent = await ctx.deps.evidence.recently_fetched(
        [h for h in hashes if h not in seen], days=DISCOVERY_DEDUP_DAYS
    )
    unseen: list[_FeedItem] = []
    for candidate in candidates:
        if candidate.link_hash in seen:
            ctx.counts["feed_seen"] += 1
        elif candidate.link_hash in recent:
            ctx.counts["recently_read"] += 1  # read through another source in 30 days
        else:
            unseen.append(candidate)
    # Newest first by `published`; an undated item keeps its feed order, after the
    # dated ones.
    unseen.sort(
        key=lambda c: (0, -c.published.timestamp(), c.index) if c.published else (1, 0.0, c.index)
    )
    take = unseen[:FEED_MAX_ITEMS_PER_RUN]
    if len(unseen) > len(take):
        ctx.counts["feed_backlog"] += len(unseen) - len(take)

    async def read_item(entry: _FeedItem) -> None:
        await _read_url(
            ctx,
            entry.link,
            trust="listed",
            source_id=source.source_id,
            tag_hints=source.tags,
            source_kind="feed_item",
            title=entry.title,
            published_at=entry.published,
            recent_days=DISCOVERY_DEDUP_DAYS,
        )

    # Several items at once (spec 2026-10-03-intel-throughput §5); the accounting is the
    # sequential loop's: items the call cap left are counted and wait for the next check.
    _settle_reads(ctx, source, await read_each(ctx, take, read_item))


async def _discovery(ctx: _Ctx) -> None:
    assert ctx.run.source_id is not None  # intel_runs CHECK: discovery has a source
    source = await ctx.deps.store.get_source(ctx.run.source_id)
    if source is None or source.query_text is None:
        ctx.counts["source_missing"] += 1
        return
    await _check_query(ctx, source)


async def _check_query(ctx: _Ctx, source: Source) -> None:
    """One check of a saved search query, recorded on the source. ``read_source`` reuses it."""
    try:
        await _discover(ctx, source)
    except _Stop as stop:
        await ctx.deps.evidence.record_check(
            source.source_id, ok=True, status=f"deferred_{stop.reason}"
        )
        raise
    await ctx.deps.evidence.record_check(source.source_id, ok=True, status="checked")


async def read_source(ctx: _Ctx, source: Source) -> None:
    """Read one registered source now, exactly as its scheduled check would, whether or not it
    is enabled: a weight suggestion's immediate read of the sources it registered (spec §4.10).
    Raises _CallCap and _Stop as a check does."""
    if source.kind == "search_query":
        if source.query_text is None:
            ctx.counts["source_missing"] += 1
            return
        await _check_query(ctx, source)
    elif source.source_url is None:
        ctx.counts["source_missing"] += 1
    else:
        await _check_listed(ctx, source, source.source_url)


async def _discover(ctx: _Ctx, source: Source) -> None:
    """One model call with web search; its candidate URLs are then fetched and read
    like any other page. The model's own search text is never evidence (#49)."""
    query = source.query_text or ""
    system, user = discovery_request(query, registry_tags=ctx.registry_tags(source.tags, query))
    call = await _call_model(ctx, lambda: ctx.deps.model.discover(system, user))
    if call.output is None:
        ctx.counts[f"model_{call.outcome}"] += 1
        return
    ctx.counts["candidates"] += len(call.output.candidates)
    # The search's own page_age for each result it returned, read against the moment it answered
    # (spec 2026-10-04-intel-evidence-quality §2): a page's date when its metadata states none.
    answered = _now(ctx)
    page_ages: dict[str, datetime] = {}
    for result in call.search_results:
        age = parse_page_age(result.page_age, answered)
        if age is not None:
            page_ages.setdefault(url_hash(result.url), age)

    urls: list[str] = []
    listed: set[str] = set()
    for candidate in call.output.candidates:
        if not _is_https(candidate.url):
            ctx.counts["not_https"] += 1
            continue
        candidate_hash = url_hash(candidate.url)
        if candidate_hash in listed:
            ctx.counts["candidate_duplicate"] += 1
            continue
        listed.add(candidate_hash)
        urls.append(candidate.url)
    recent = await ctx.deps.evidence.recently_fetched(
        [url_hash(u) for u in urls], days=DISCOVERY_DEDUP_DAYS
    )
    todo = [u for u in urls if url_hash(u) not in recent]
    ctx.counts["recently_read"] += len(urls) - len(todo)

    async def read_result(url: str) -> None:
        await _read_url(
            ctx,
            url,
            trust="web",
            source_id=source.source_id,
            tag_hints=source.tags,
            source_kind="web_search_result",
            recent_days=DISCOVERY_DEDUP_DAYS,
            page_age=page_ages.get(url_hash(url)),
        )

    # The search is one call; the pages it found are read several at once (spec
    # 2026-10-03-intel-throughput §5).
    _settle_reads(ctx, source, await read_each(ctx, todo, read_result))


async def _adhoc(ctx: _Ctx) -> None:
    """A URL an operator pasted: ``trust = listed``, because a person chose it. It
    is read even if read recently -- they asked."""
    url = ctx.run.request.get("url")
    if not isinstance(url, str) or not url.strip():
        raise _Stop("request_missing_url", gate=False)
    await _read_url(
        ctx, url, trust="listed", source_id=None, tag_hints=(), source_kind="operator_pasted"
    )


# ── one page ───────────────────────────────────────────────────────────────────


async def _read_url(
    ctx: _Ctx,
    url: str,
    *,
    trust: Trust,
    source_id: UUID | None,
    tag_hints: Sequence[str],
    source_kind: str,
    title: str = "",
    published_at: datetime | None = None,
    recent_days: int | None = None,
    page_age: datetime | None = None,
) -> None:
    """Fetch and read one page as one unit. ``recent_days`` skips a page whose
    redirect lands on a document already read in that window (feeds, discovery).
    ``published_at`` is a feed item's own date, ``page_age`` a web search's date for the
    page (spec 2026-10-04-intel-evidence-quality §2)."""
    if not _is_https(url):
        ctx.counts["not_https"] += 1
        return
    if await _known_hit(ctx, url):
        return
    if not ctx.calls_left():
        raise _CallCap  # before the fetch: the unit is left untouched
    fetched = await _fetch(ctx, url)
    if isinstance(fetched, FetchFailure):
        return  # counted; unconsumed, so the next check retries it
    if await _known_hit_after_redirect(ctx, url, fetched.final_url):
        return
    final_hash = url_hash(fetched.final_url)
    if recent_days is not None and final_hash != url_hash(url):
        recent = await ctx.deps.evidence.recently_fetched([final_hash], days=recent_days)
        if final_hash in recent:
            ctx.counts["recently_read"] += 1
            return
    text, truncated = _bounded(ctx, normalise(fetched.text), fetched.truncated)
    masked, _ = mask(text)
    await _extract_unit(
        ctx,
        requested_url=url,
        fetched=fetched,
        text=text,
        model_text=masked,
        truncated=truncated,
        source_id=source_id,
        trust=trust,
        tag_hints=tag_hints,
        source_kind=source_kind,
        snapshot=None,
        source_hash=None,
        title=title,
        published_at=published_at,
        page_age=page_age,
    )


async def _extract_unit(
    ctx: _Ctx,
    *,
    requested_url: str,
    fetched: TextFetch,
    text: str,
    model_text: str,
    truncated: bool,
    source_id: UUID | None,
    trust: Trust,
    tag_hints: Sequence[str],
    source_kind: str,
    snapshot: SnapshotRecord | None,
    source_hash: tuple[UUID, str] | None,
    title: str = "",
    published_at: datetime | None = None,
    page_age: datetime | None = None,
) -> None:
    """One metered extraction, local verification, and the unit's one transaction.

    ``text`` is the UNMASKED normalised text every quote is verified against;
    ``model_text`` is what the model reads (masked, or a masked policy diff).

    The document's publication date (spec 2026-10-04-intel-evidence-quality §2) is the first
    plausible of: the feed item's ``published_at``, the page's metadata (``fetched``), the
    search's ``page_age``, and a date the model reports that the text states. Never the fetch
    time; none of them leaves the document undated."""
    requested_hash, final_hash = url_hash(requested_url), url_hash(fetched.final_url)
    if await ctx.deps.evidence.recorded_in_run(ctx.run.run_id, [requested_hash, final_hash]):
        ctx.counts["already_recorded"] += 1  # a reclaimed run: never billed twice
        return

    system, user = extraction_request(
        model_text,
        source_kind=source_kind,
        tag_hints=tuple(tag_hints),
        registry_tags=ctx.registry_tags(tag_hints, model_text),
    )
    call = await _call_model(ctx, lambda: ctx.deps.model.extract(system, user))

    signals: list[SignalRecord] = []
    if call.output is None:
        ctx.counts[f"model_{call.outcome}"] += 1  # a verdict, not an outage: consumed
    else:
        signals = [
            record
            for extracted in call.output.signals
            if (record := _verified_signal(ctx, extracted, text, call.answered_by)) is not None
        ]

    masked_title, title_masks = mask(title)
    if title_masks:
        ctx.counts["pii_masked_title"] += 1
    published, published_from = choose_published(
        feed=published_at,
        metadata=fetched.published_at,
        page_age=page_age,
        stated=stated_date(call.output.published_date, text) if call.output is not None else None,
        now=_now(ctx),
    )
    ctx.counts[f"published_from_{published_from}"] += 1
    document = DocumentRecord(
        run_id=ctx.run.run_id,
        source_id=source_id,
        document_url=canonicalise(requested_url),
        document_url_hash=requested_hash,
        final_url=fetched.final_url,
        url_hash=final_hash,
        publisher_domain=publisher_domain(fetched.final_url),
        trust=trust,
        content_sha256=content_sha256(text),
        truncated=truncated,
        title=masked_title,
        published_at=published,
    )
    recorded = await ctx.deps.evidence.record_unit(
        document, signals, snapshot=snapshot, source_hash=source_hash
    )
    if recorded is None:
        ctx.counts["already_recorded"] += 1
        return
    ctx.counts["documents_recorded"] += 1
    ctx.counts["signals_kept"] += len(signals)


def _verified_signal(
    ctx: _Ctx, extracted: ExtractedSignal, text: str, model_id: str
) -> SignalRecord | None:
    """#49 in code, not in the prompt: quotes verbatim or dropped, tags registered or
    dropped, free text masked. A signal with no surviving quote does not exist."""
    quotes: dict[tuple[int, int], VerifiedQuote] = {}
    for raw in extracted.quotes:
        verified = verify_quote(text, raw)
        if isinstance(verified, VerifiedQuote):
            quotes.setdefault((verified.char_start, verified.char_end), verified)
        else:
            ctx.counts[f"quote_dropped_{verified}"] += 1
    if not quotes:
        ctx.counts["signal_dropped_no_quote"] += 1
        return None

    tags: list[str] = []
    for tag in extracted.tags:
        if ctx.registry is not None and tag in ctx.registry.retired:
            ctx.counts["tag_dropped_retired_tag"] += 1
        elif ctx.registry is None or tag not in ctx.registry.active:
            ctx.counts["tag_dropped_unknown_tag"] += 1  # never turned into a subject
        elif not is_well_formed(tag):
            ctx.counts["tag_dropped_malformed"] += 1
        elif tag in tags:
            ctx.counts["tag_dropped_duplicate"] += 1
        else:
            tags.append(tag)

    summary, summary_masks = mask(extracted.summary)
    if summary_masks:
        ctx.counts["pii_masked_summary"] += 1
    if len(summary) > _SUMMARY_MAX_CHARS:
        summary = summary[:_SUMMARY_MAX_CHARS]
        ctx.counts["summary_truncated"] += 1

    subjects: list[str] = []
    for raw_subject in extracted.unregistered_subjects:
        subject, subject_masks = mask(raw_subject)
        if subject_masks:
            ctx.counts["pii_masked_unregistered_subjects"] += 1
        subject = normalise(subject)
        if subject and subject not in subjects:
            subjects.append(subject)

    return SignalRecord(
        category=extracted.category,
        direction=extracted.direction,
        tags=tuple(tags),
        unregistered_subjects=tuple(subjects),
        summary=summary,
        model_id=model_id,
        prompt_version=EXTRACT_PROMPT_VERSION,
        quotes=tuple(sorted(quotes.values(), key=lambda q: q.char_start)),
    )


# ── proposal generation (steps 2 and 3) ────────────────────────────────────────


async def _generate(ctx: _Ctx) -> None:
    """spec §4.3: weight_change, coverage_gap and threat_event proposals, plus attach. A
    gap_regenerate run's evidence is the signals its request names, and it proposes events
    only (§4.9). Raises _Stop only for a gate skip or an unavailable model; everything the
    model can SAY is consumed."""
    store = ctx.deps.proposals
    regenerate = ctx.regenerate
    if regenerate is not None:
        new = await store.signals_by_id(regenerate.signal_ids)
        if not new:
            ctx.counts["gap_regenerate_no_active_signals"] += 1
            return
    else:
        new = await store.run_signals(ctx.run.run_id)
        if not new:
            return
    if await store.proposals_written(ctx.run.run_id):
        ctx.counts["proposals_already_written"] += 1
        return
    vocabulary = parse_vocabulary(ctx.vocabulary) if ctx.vocabulary is not None else None
    if vocabulary is None:
        # Every kind needs it: a weight change its cells, a threat event its registered tags
        # (spec §3.8, corrected 2026-09-30).
        ctx.counts["proposals_skipped_vocabulary_missing"] += 1
        return
    now = ctx.deps.clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    run_signal_ids = [s.signal_id for s in new]
    new = _newest(new, ctx)
    tags = sorted({t for s in new for t in s.tags})
    if regenerate is not None and regenerate.tag not in tags:
        # A gap's signals name the subject as unregistered, so they carry no tag: the newly
        # mapped tag is what finds related events and pending proposals.
        tags = sorted([*tags, regenerate.tag])
    related = await store.related_signals(
        exclude=run_signal_ids,
        tags=tags,
        categories=sorted({s.category for s in new}),
        since=now - timedelta(days=PROPOSAL_CONTEXT_DAYS),
        limit=PROPOSAL_CONTEXT_MAX_SIGNALS,
    )
    registry = vocabulary.registry()
    gap_pool = await store.gap_candidates(
        since=now - timedelta(days=COVERAGE_GAP_WINDOW_DAYS),
        unmapped_tags=sorted((registry.active | registry.retired) - vocabulary.mapped_tags),
        limit=COVERAGE_GAP_POOL_MAX,
    )
    pending_events = await store.pending_event_proposals(
        tags=tags, limit=PROPOSAL_CONTEXT_MAX_EVENTS
    )
    live_events = await store.active_threat_events(tags=tags, limit=PROPOSAL_CONTEXT_MAX_EVENTS)
    live_protections = await store.active_protection_events(
        tags=tags, limit=PROPOSAL_CONTEXT_MAX_EVENTS
    )
    relevant = set(tags) | {t for s in related for t in s.tags} | set(vocabulary.mapped_tags)
    system, user = proposal_request(
        [prompt_signal(s) for s in new],
        [prompt_signal(s) for s in related],
        quiz=prompt_quiz(vocabulary),
        registry_tags=prompt_registry(vocabulary, relevant),
        mapped_tags=sorted(vocabulary.mapped_tags),
        pending_events=[prompt_pending_event(e) for e in pending_events],
        live_events=[prompt_live_event(e) for e in live_events],
        live_protections=[prompt_live_protection(e) for e in live_protections],
        events_only=regenerate is not None,
    )
    call = await _call_model(ctx, lambda: ctx.deps.model.propose(system, user), capped=False)
    batch = GeneratedBatch()
    if call.output is None:
        ctx.counts[f"proposal_model_{call.outcome}"] += 1  # a verdict, not an outage: consumed
    else:
        batch = validate_proposals(
            call.output,
            context={s.signal_id: s for s in [*new, *related]},
            gap_pool=gap_pool,
            vocabulary=vocabulary,
            now=now,
            counts=ctx.counts,
            pending_events={e.proposal_id: e for e in pending_events},
            new_signal_ids=frozenset(s.signal_id for s in new),
            events_only=regenerate is not None,
        )
    result = await store.write_generated(
        ctx.run.run_id,
        batch.proposals,
        against_scoring_version=vocabulary.scoring_version,
        against_release_no=vocabulary.release_no,
        model_id=call.answered_by,
        prompt_version=PROPOSE_PROMPT_VERSION,
        attachments=batch.attachments,
    )
    if result is None:
        ctx.counts["proposals_already_written"] += 1
        return
    ctx.counts["proposals_written"] += len(result.written)
    ctx.counts["proposals_superseded"] += len(result.superseded)
    if result.attached:
        ctx.counts["proposals_attached"] += len(result.attached)
    if result.attach_dropped:
        ctx.counts["attach_dropped_not_pending"] += result.attach_dropped


def _newest(signals: list[ContextSignal], ctx: _Ctx) -> list[ContextSignal]:
    """At most PROPOSAL_CONTEXT_MAX_NEW_SIGNALS of a run's new signals, the newest, in the order
    given; the excess is counted on the run's outcome (final review M7). What the prompt is not
    shown, the model cannot cite or attach, and it is left out of related evidence too."""
    excess = len(signals) - PROPOSAL_CONTEXT_MAX_NEW_SIGNALS
    if excess <= 0:
        return signals
    ctx.counts["proposal_new_signals_over_cap"] += excess
    newest = sorted(signals, key=lambda s: (s.created_at, s.signal_id), reverse=True)
    kept = {s.signal_id for s in newest[:PROPOSAL_CONTEXT_MAX_NEW_SIGNALS]}
    return [s for s in signals if s.signal_id in kept]


# ── protection renewal (step 4) ────────────────────────────────────────────────


async def _renewal_check(ctx: _Ctx) -> RunResult:
    """spec §4.8: fetch the pages behind a due credit's cited excerpts again, re-verify each
    verbatim (#49), and write a pending renewal from what still verifies (intel/renewal.py).
    NO model call, so no provider gate: the budget and the kill switch do not stop it, and it
    costs nothing but fetches. A fetcher outage fails the run, and the worker queues it again."""
    try:
        request = RenewalRequest.model_validate(ctx.run.request)
    except ValidationError:
        return RunResult("failed", ctx.outcome(), "request_unreadable")
    if await ctx.deps.proposals.proposals_written(ctx.run.run_id):
        ctx.counts["proposals_already_written"] += 1  # a reclaimed run whose write committed
        return RunResult("completed", ctx.outcome())
    now = ctx.deps.clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    protections = ctx.deps.protections
    evidence = await protections.renewal_evidence(request.event_id, now=now)
    if evidence is None:  # retracted, renewed, lapsed or already given a renewal proposal
        ctx.counts[RENEWAL_NOT_DUE] += 1
        return RunResult("completed", ctx.outcome())
    pages: dict[str, RenewalPage | str] = {}
    by_final: dict[str, RenewalPage] = {}
    # One page after another, on purpose (spec 2026-10-03-intel-throughput §5): a renewal makes no
    # model call, so its pages are seconds of fetching, and by_final shares a page two cited
    # URLs reach, which reads in parallel would fetch twice.
    try:
        for signal in evidence.signals:
            if signal.document_url_hash not in pages:
                pages[signal.document_url_hash] = await _renewal_page(
                    ctx, signal.document_url, by_final
                )
    except _Stop as stop:  # the fetcher itself is down: says nothing about the evidence
        ctx.counts[f"stopped_{stop.reason}"] += 1
        return RunResult("failed", ctx.outcome(), stop.reason)
    plan = plan_renewal(evidence, pages)
    ctx.counts["renewal_excerpts_checked"] += plan.excerpts_checked
    ctx.counts["renewal_excerpts_verified"] += plan.excerpts_verified
    for reason, count in plan.dropped.items():
        ctx.counts[f"renewal_excerpt_dropped_{reason}"] += count
    if not evidence.signals:
        ctx.counts["renewal_no_active_signals"] += 1
    if not plan.signals:
        gone = RENEWAL_EVIDENCE_UNREACHABLE if plan.unreachable else RENEWAL_EVIDENCE_GONE
        ctx.counts[gone] += 1
        return RunResult("completed", ctx.outcome())
    written = await protections.write_renewal(ctx.run.run_id, evidence, plan, now=now)
    if written.status == "already_written":
        ctx.counts["proposals_already_written"] += 1
    elif written.status == "not_due":
        ctx.counts[RENEWAL_NOT_DUE] += 1
    else:
        ctx.counts[RENEWAL_PROPOSED] += 1
        ctx.counts["proposals_written"] += 1
        ctx.counts["renewal_signals_reverified"] += len(plan.signals)
    return RunResult("completed", ctx.outcome())


async def _renewal_page(
    ctx: _Ctx, url: str, by_final: dict[str, RenewalPage]
) -> RenewalPage | str:
    """One cited page fetched again under every guard extraction uses: https only, never a known
    hit location (before the fetch, and again on the final URL), the document cap. A page two
    cited URLs now reach is read once and shared. Returns the page, or why it was not read."""
    if not _is_https(url):
        ctx.counts["not_https"] += 1
        return "not_https"
    if await _known_hit(ctx, url):
        return "known_hit_location"
    fetched = await _fetch(ctx, url)  # a fetcher-side failure raises _Stop
    if isinstance(fetched, FetchFailure):
        return f"fetch_{fetched.code}"
    if await _known_hit_after_redirect(ctx, url, fetched.final_url):
        return "known_hit_location"
    final_hash = url_hash(fetched.final_url)
    if final_hash in by_final:
        return by_final[final_hash]
    text, truncated = _bounded(ctx, normalise(fetched.text), fetched.truncated)
    page = RenewalPage(
        requested_url=url, final_url=fetched.final_url, text=text, truncated=truncated
    )
    by_final[final_hash] = page
    return page


# ── helpers ────────────────────────────────────────────────────────────────────


def _now(ctx: _Ctx) -> datetime:
    now = ctx.deps.clock()
    return now if now.tzinfo is not None else now.replace(tzinfo=UTC)


async def _call_model(
    ctx: _Ctx, call: Callable[[], Awaitable[ModelCall[T]]], *, capped: bool = True
) -> ModelCall[T]:
    """One call through the provider gate. A gate refusal or an unavailable model stops the
    run with the current unit unconsumed; every returned answer -- including a refusal or
    unparseable output -- is handed back to be consumed. ``capped=False`` is the generation
    call: one per run, on top of INTEL_MAX_CALLS_PER_RUN.

    *Amended 2026-10-03 (throughput, spec 2026-10-03-intel-throughput §5):* a capped call
    RESERVES its slot before it is sent -- the check and the count happen with no ``await``
    between them -- so reads running side by side can never send more calls than the cap
    between them. A call the gate does not send gives its slot back; one sent and failed keeps
    it, as before. A web search's ``pause_turn`` continuations are counted when it returns."""
    reserved = False
    if capped:
        if not ctx.calls_left():
            raise _CallCap
        ctx.model_calls += 1
        reserved = True
    try:
        outcome, result, reason = await metered(
            ctx.deps.control, run_id=ctx.run.run_id, now=ctx.deps.clock(), call=call
        )
    except BaseException:
        if reserved:
            ctx.model_calls -= 1
        raise
    if outcome != "called":  # "skipped" (the gate) or "budget_unset"
        if reserved:
            ctx.model_calls -= 1  # nothing was sent
        raise _Stop(reason or outcome, gate=True)
    if not reserved:
        ctx.model_calls += 1
    if result is None:  # ModelUnavailable: timeout / error / rate_limited
        raise _Stop(reason or "error", gate=False)
    ctx.model_calls += result.pause_turns  # web-search continuations are calls too
    ctx.cost_usd += result.cost_usd
    return result


# ── several reads at once (spec 2026-10-03-intel-throughput §5) ────────────────

_X = TypeVar("_X")
# Set inside each lane of read_each: a read_each nested in one (a feed's items, read as one of a
# weight suggestion's sources) runs its items one after another in that lane, so one run never
# has more than INTEL_SOURCE_READ_CONCURRENCY reads in flight however the reads nest.
_in_read_lane: ContextVar[bool] = ContextVar("_in_read_lane", default=False)


@dataclass(frozen=True)
class ReadEach(Generic[_X]):
    """What ``read_each`` did with each item, in the items' order. ``raised[i]`` is the exception
    item ``i``'s read raised (None when it finished); ``started[i]`` is False for an item that
    never started, because a read stopped the run or the call cap was reached first."""

    items: tuple[_X, ...]
    started: tuple[bool, ...]
    raised: tuple[Exception | None, ...]

    def first_stop(self) -> _Stop | None:
        return next((e for e in self.raised if isinstance(e, _Stop)), None)

    def first_bug(self) -> Exception | None:
        """An exception that is no outcome of reading at all: the run crashes on it, as it did
        when reads ran one after another."""
        return next(
            (e for e in self.raised if e is not None and not isinstance(e, _Stop | _CallCap)),
            None,
        )


async def read_each(
    ctx: _Ctx,
    items: Sequence[_X],
    read: Callable[[_X], Awaitable[None]],
    *,
    while_calls_left: bool = True,
) -> ReadEach[_X]:
    """``read`` every item, at most ``INTEL_SOURCE_READ_CONCURRENCY`` at once, started in the
    items' order. An item starts only while the run has calls left (``calls_left``) and no read
    has raised -- a stop (``_Stop``), the call cap (``_CallCap``) or a bug; after that, nothing
    new starts and the reads already in flight finish. A read that fails never cancels another:
    every exception is kept in its item's slot for the caller to account for. One item, or a
    concurrency of one, reads exactly as the sequential loop did. ``while_calls_left=False`` is
    stage 3's validation: every candidate gets a verdict, the cap's included, so the cap stops
    nothing from starting there."""
    limit = 1 if _in_read_lane.get() else max(1, ctx.deps.source_read_concurrency)
    started = [False] * len(items)
    raised: list[Exception | None] = [None] * len(items)
    cursor = 0
    halted = False

    async def lane() -> None:
        nonlocal cursor, halted
        _in_read_lane.set(True)  # this lane's own context: gather runs each lane in a copy
        while not halted and cursor < len(items):
            if while_calls_left and not ctx.calls_left():
                halted = True
                return
            index = cursor
            cursor += 1
            started[index] = True
            try:
                await read(items[index])
            except Exception as exc:
                raised[index] = exc
                halted = True  # a stop, the cap or a bug: nothing new starts after it

    await asyncio.gather(*(lane() for _ in range(min(limit, len(items)))))
    return ReadEach(tuple(items), tuple(started), tuple(raised))


def _settle_reads(ctx: _Ctx, source: Source, each: ReadEach[Any]) -> None:
    """The sequential loop's accounting for one source's pages read side by side: a bug is
    raised, items the call cap left (refused by it, or never started under it) are
    ``call_cap_deferred`` and mark the source ``partly_read``, and a stop is raised once every
    read in flight has finished. Items left unstarted by a stop are not counted, as before."""
    bug = each.first_bug()
    if bug is not None:
        raise bug
    stop = each.first_stop()
    left = sum(isinstance(e, _CallCap) for e in each.raised)
    if stop is None:
        left += each.started.count(False)
    if left:
        ctx.counts["call_cap_deferred"] += left
        ctx.counts["stopped_call_cap"] += 1
        ctx.partly_read.add(source.source_id)
    if stop is not None:
        raise stop


async def _fetch(ctx: _Ctx, url: str) -> TextFetch | FetchFailure:
    fetched = await ctx.deps.fetcher.fetch_text(url)
    if isinstance(fetched, FetchFailure):
        ctx.counts[f"fetch_{fetched.code}"] += 1
        if fetched.code in FETCHER_SIDE_CODES:
            raise _Stop(fetched.code, gate=False)  # ours: says nothing about the page
        return fetched
    ctx.counts["documents_fetched"] += 1
    return fetched


async def _known_hit(ctx: _Ctx, url: str) -> bool:
    if await ctx.deps.store.is_known_hit(url_hash(url)):
        ctx.counts["known_hit_location"] += 1
        return True
    return False


async def _known_hit_after_redirect(ctx: _Ctx, requested: str, final: str) -> bool:
    """The known-hit check again on ``final_url``, before anything is read. Skipped
    only when the fetch did not move (same canonical hash as the one checked)."""
    if url_hash(final) == url_hash(requested):
        return False
    return await _known_hit(ctx, final)


def _bounded(ctx: _Ctx, full: str, fetched_truncated: bool) -> tuple[str, bool]:
    """``INTEL_MAX_DOCUMENT_CHARS``: past it only the head is read and the document
    is marked ``truncated`` (spec §4.3 step 6)."""
    limit = ctx.deps.max_document_chars
    if len(full) > limit:
        ctx.counts["document_truncated"] += 1
        return full[:limit], True
    return full, fetched_truncated


def _is_https(url: str) -> bool:
    try:
        return urlsplit(url.strip()).scheme.lower() == "https"
    except ValueError:
        return False


def _published(value: object) -> datetime | None:
    """A feed item's ``published`` as an aware datetime, or None (undated). The
    fetcher hands back ISO strings, some NAIVE (RFC-822 ``-0000``), which are UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def changed_hunks(before: str, after: str, *, max_chars: int) -> str:
    """The sentences of ``after`` that are new or changed since ``before``, each run
    with ``_DIFF_CONTEXT_SENTENCES`` of context either side, joined by a separator.

    Both inputs are MASKED normalised text (single spaces, so a sentence boundary is
    exactly ``". "`` and rejoining a run reproduces it verbatim -- a quote inside one
    run is still a substring of the fetched text). Sentence-level, never
    character-level: a SequenceMatcher over 200 000 characters is quadratic. A pure
    deletion keeps the context around where the text went. Past
    ``_DIFF_MAX_SENTENCES`` the whole text is sent instead. Pure and synchronous: the
    pipeline runs it with ``asyncio.to_thread``."""
    old = _SENTENCE_END.split(before)
    new = _SENTENCE_END.split(after)
    if len(old) > _DIFF_MAX_SENTENCES or len(new) > _DIFF_MAX_SENTENCES:
        return after[:max_chars]
    keep: set[int] = set()
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        low = max(0, j1 - _DIFF_CONTEXT_SENTENCES)
        high = min(len(new), max(j1, j2) + _DIFF_CONTEXT_SENTENCES)
        keep.update(range(low, high))
    if not keep:
        return after[:max_chars]
    runs: list[list[int]] = []
    for index in sorted(keep):
        if runs and runs[-1][-1] == index - 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    joined = _HUNK_SEPARATOR.join(" ".join(new[i] for i in run_) for run_ in runs)
    return joined[:max_chars]


__all__ = [
    "PipelineDeps",
    "ReadEach",
    "RunResult",
    "changed_hunks",
    "read_each",
    "read_source",
    "run",
]
