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
"""

from __future__ import annotations

import asyncio
import difflib
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

import structlog
from pydantic import BaseModel

from imageshield.intel.bounds import (
    DISCOVERY_DEDUP_DAYS,
    FEED_MAX_ITEM_AGE_DAYS,
    FEED_MAX_ITEMS_PER_RUN,
    MAX_PROMPT_TAGS,
    MIN_POLICY_TEXT_CHARS,
)
from imageshield.intel.evidence_store import (
    DocumentRecord,
    EvidenceStore,
    SignalRecord,
    SnapshotRecord,
)
from imageshield.intel.fetch_client import FETCHER_SIDE_CODES, FetchFailure, TextFetch, TextFetcher
from imageshield.intel.metering import metered
from imageshield.intel.model import IntelModel, ModelCall
from imageshield.intel.models import Run, Source, Vocabulary
from imageshield.intel.pii import mask
from imageshield.intel.prompts import (
    EXTRACT_PROMPT_VERSION,
    RegistryTag,
    discovery_request,
    extraction_request,
)
from imageshield.intel.publisher import publisher_domain
from imageshield.intel.schemas import ExtractedSignal
from imageshield.intel.store import IntelStore
from imageshield.intel.tags import TagRegistry, is_well_formed
from imageshield.intel.text import content_sha256, normalise
from imageshield.intel.verify import VerifiedQuote, verify_quote
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


@dataclass
class PipelineDeps:
    """Everything a run touches. Mutable on purpose: the worker's tests move
    ``clock`` past a lease. ``max_calls_per_run`` and ``max_document_chars`` are
    ``INTEL_MAX_CALLS_PER_RUN`` / ``INTEL_MAX_DOCUMENT_CHARS`` and have NO default
    here -- a second default beside IntelConfig's would be a second source of truth."""

    store: IntelStore
    evidence: EvidenceStore
    fetcher: TextFetcher
    model: IntelModel
    control: ProviderControlStore
    clock: Callable[[], datetime]
    max_calls_per_run: int
    max_document_chars: int


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    outcome: dict[str, int | str]
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

    def calls_left(self) -> bool:
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
    try:
        if claimed.kind == "source_check":
            await _source_check(ctx)
        elif claimed.kind == "discovery":
            await _discovery(ctx)
        elif claimed.kind == "adhoc_url":
            await _adhoc(ctx)
        else:  # weight_suggestion / renewal_check / gap_regenerate arrive in later steps
            return RunResult("failed", ctx.outcome(), "kind_not_supported_yet")
    except _CallCap:
        ctx.counts["stopped_call_cap"] += 1
    except _Stop as stop:
        ctx.counts[f"stopped_{stop.reason}"] += 1
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
    # Before the fetch, not only after it: a listed URL that IS a known hit location
    # is never requested at all (spec §4.3, §6.1).
    if await _known_hit(ctx, source.source_url):
        await ctx.deps.evidence.record_check(source.source_id, ok=True, status="known_hit_location")
        return
    fetched = await _fetch(ctx, source.source_url)  # a fetcher-side failure raises _Stop
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
    for position, entry in enumerate(take):
        try:
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
        except _CallCap:
            ctx.counts["call_cap_deferred"] += len(take) - position
            ctx.counts["stopped_call_cap"] += 1
            return


async def _discovery(ctx: _Ctx) -> None:
    assert ctx.run.source_id is not None  # intel_runs CHECK: discovery has a source
    source = await ctx.deps.store.get_source(ctx.run.source_id)
    if source is None or source.query_text is None:
        ctx.counts["source_missing"] += 1
        return
    try:
        await _discover(ctx, source)
    except _Stop as stop:
        await ctx.deps.evidence.record_check(
            source.source_id, ok=True, status=f"deferred_{stop.reason}"
        )
        raise
    await ctx.deps.evidence.record_check(source.source_id, ok=True, status="checked")


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
    for position, url in enumerate(todo):
        try:
            await _read_url(
                ctx,
                url,
                trust="web",
                source_id=source.source_id,
                tag_hints=source.tags,
                source_kind="web_search_result",
                recent_days=DISCOVERY_DEDUP_DAYS,
            )
        except _CallCap:
            ctx.counts["call_cap_deferred"] += len(todo) - position
            ctx.counts["stopped_call_cap"] += 1
            return


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
) -> None:
    """Fetch and read one page as one unit. ``recent_days`` skips a page whose
    redirect lands on a document already read in that window (feeds, discovery)."""
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
) -> None:
    """One metered extraction, local verification, and the unit's one transaction.

    ``text`` is the UNMASKED normalised text every quote is verified against;
    ``model_text`` is what the model reads (masked, or a masked policy diff)."""
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
        published_at=published_at,
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


# ── helpers ────────────────────────────────────────────────────────────────────


async def _call_model(ctx: _Ctx, call: Callable[[], Awaitable[ModelCall[T]]]) -> ModelCall[T]:
    """One call through the provider gate. A gate refusal or an unavailable model
    stops the run with the current unit unconsumed; every returned answer --
    including a refusal or unparseable output -- is handed back to be consumed."""
    if not ctx.calls_left():
        raise _CallCap
    outcome, result, reason = await metered(
        ctx.deps.control, run_id=ctx.run.run_id, now=ctx.deps.clock(), call=call
    )
    if outcome != "called":  # "skipped" (the gate) or "budget_unset"
        raise _Stop(reason or outcome, gate=True)
    ctx.model_calls += 1
    if result is None:  # ModelUnavailable: timeout / error / rate_limited
        raise _Stop(reason or "error", gate=False)
    ctx.model_calls += result.pause_turns  # web-search continuations are calls too
    ctx.cost_usd += result.cost_usd
    return result


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


__all__ = ["PipelineDeps", "RunResult", "changed_hunks", "run"]
