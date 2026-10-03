"""The run log: one row per step of an intel run, written as it happens (spec 2026-10-03 §3).

A Suggest-points question is ONE Claude call of several minutes. Without this, nothing was known
until it returned, and a failure left only an exception class behind. The worker binds a
:class:`RunLog` for the duration of ``run(claimed, deps)`` (``current_run_log``); the model seam
(``intel/model.py``) streams every call through a :class:`StreamTranslator`, and ``metered()``
notes how each call ended. The control room polls ``GET /v1/admin/intel/runs/{run_id}/events``.

**The vocabulary is closed** (``RunEventKind``, and migration 0046's CHECK). The console renders
on ``kind`` and the ``detail`` keys, never on ``text``: ``text`` is server-authored English for
display and may change wording.

**Nothing here can fail a run.** A write that errors logs ``intel.run_log_write_failed`` (the run
id and the kind, nothing else) and the run carries on. With no log bound -- tests, scripts, a
pipeline call outside the worker -- every call is a no-op.

**What a row may hold:** the model's own search queries, the titles and URLs of public search
results, a reasoning SUMMARY, counts, costs, model ids and API error text. Intel prompts carry no
personal data (spec §6.1), and nothing here ever reads a prompt or the model's answer.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_args
from urllib.parse import urlsplit
from uuid import UUID

import structlog

if TYPE_CHECKING:
    from imageshield.intel.model import ModelCall, ModelUnavailable
    from imageshield.intel.models import Run

log = structlog.get_logger("imageshield.intel.run_log")

RunEventKind = Literal[
    "run_started",
    "model_call_started",
    "thinking",
    "search",
    "search_results",
    "writing",
    "continuing",
    "model_call_finished",
    "model_call_failed",
    "model_call_skipped",
    "run_finished",
    "truncated",
]
RUN_EVENT_KINDS: tuple[str, ...] = get_args(RunEventKind)

# The caps (spec §3.3). Not thresholds of the identity path: they bound what one run may write.
MAX_EVENTS_PER_RUN = 300  # the last row is `truncated`, then nothing more
MAX_TEXT_CHARS = 2000
MAX_THINKING_CHARS = 4000  # migration 0046's CHECK on intel_run_events.text
MAX_DETAIL_BYTES = 8 * 1024  # serialised; `results` is dropped first, then `{"oversize": true}`
THINKING_UPDATE_SECONDS = 2.0  # a streaming thinking row is rewritten at most this often
MAX_LOGGED_SEARCH_RESULTS = 10
MAX_RESULT_TITLE_CHARS = 300
MAX_FAILURE_MESSAGE_CHARS = 500  # ModelUnavailable.message, the API's own words
SEARCH_RESULT_DOMAINS_SHOWN = 3

TRUNCATED_TEXT = "Log limit reached; later steps were not recorded"
WRITING_TEXT = "Writing the answer"
CONTINUING_TEXT = "Continuing — Claude paused after its searches"


class RunEventStore(Protocol):
    """The three writes the recorder needs (``intel/store.py`` implements them). Each is one short
    statement on its own connection, committed at once and never inside a run's transaction, so a
    poll sees a step the moment it is written."""

    async def last_run_event_seq(self, run_id: UUID) -> int: ...
    async def insert_run_event(
        self, run_id: UUID, *, seq: int, kind: str, text: str, detail: Mapping[str, Any]
    ) -> None: ...
    async def update_run_event(
        self, run_id: UUID, *, seq: int, text: str, detail: Mapping[str, Any]
    ) -> None: ...


class RunLog:
    """One run's log. ``seq`` continues from the run's ``max(seq) + 1``, read before this log's
    first write, so a run reclaimed after a crash appends rather than collides. The
    ``MAX_EVENTS_PER_RUN``-th row is ``truncated`` and nothing is appended after it."""

    def __init__(self, run_id: UUID, store: RunEventStore) -> None:
        self.run_id = run_id
        self._store = store
        self._last: int | None = None  # the highest seq known written; None reads it again
        self._full = False
        self._kinds: dict[int, str] = {}

    async def append(
        self, kind: RunEventKind, text: str, detail: Mapping[str, Any] | None = None
    ) -> int | None:
        """Write one row; its ``seq``, or None when nothing (or only ``truncated``) was written."""
        if self._full:
            return None
        try:
            if self._last is None:
                self._last = await self._store.last_run_event_seq(self.run_id)
            seq = self._last + 1
            if seq > MAX_EVENTS_PER_RUN:
                self._full = True
                return None
            if seq == MAX_EVENTS_PER_RUN:
                await self._store.insert_run_event(
                    self.run_id, seq=seq, kind="truncated", text=TRUNCATED_TEXT, detail={}
                )
                self._last = seq
                self._full = True
                return None
            await self._store.insert_run_event(
                self.run_id,
                seq=seq,
                kind=kind,
                text=_capped_text(kind, text),
                detail=capped_detail(detail or {}),
            )
        except Exception:
            # Read the run's max(seq) again before the next write: a failed insert may have
            # collided with a row this log did not know about.
            self._last = None
            log.warning("intel.run_log_write_failed", run_id=str(self.run_id), kind=kind)
            return None
        self._last = seq
        self._kinds[seq] = kind
        return seq

    async def update(self, seq: int, text: str, detail: Mapping[str, Any] | None = None) -> None:
        """Rewrite a row this log wrote (a streaming ``thinking`` row). Allowed after the log is
        full: it adds no row."""
        kind = self._kinds.get(seq, "thinking")
        try:
            await self._store.update_run_event(
                self.run_id,
                seq=seq,
                text=_capped_text(kind, text),
                detail=capped_detail(detail or {}),
            )
        except Exception:
            log.warning("intel.run_log_write_failed", run_id=str(self.run_id), kind=kind)


# The worker binds one for the duration of `run(claimed, deps)`; absent, every call is a no-op.
current_run_log: ContextVar[RunLog | None] = ContextVar("current_run_log", default=None)


def _capped_text(kind: str, text: str) -> str:
    limit = MAX_THINKING_CHARS if kind == "thinking" else MAX_TEXT_CHARS
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _encoded_size(detail: Mapping[str, Any]) -> int:
    return len(json.dumps(detail, ensure_ascii=False).encode("utf-8"))


def capped_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
    """JSON-safe (a Decimal or a UUID becomes its string) and at most ``MAX_DETAIL_BYTES``
    serialised: ``results`` is dropped first, then the whole detail is ``{"oversize": true}``."""
    safe: dict[str, Any] = json.loads(json.dumps(dict(detail), default=str))
    if _encoded_size(safe) <= MAX_DETAIL_BYTES:
        return safe
    safe.pop("results", None)
    if _encoded_size(safe) <= MAX_DETAIL_BYTES:
        return safe
    return {"oversize": True}


# ── the vocabulary's words (spec §3.2) ─────────────────────────────────────────────────────────


def duration_words(ms: int) -> str:
    """``0.4s``, ``45s``, ``3m 12s``, ``1h 4m``."""
    ms = max(0, ms)
    if ms < 10_000:
        return f"{ms / 1000:.1f}s"
    seconds = round(ms / 1000)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def money_words(amount: Decimal) -> str:
    """Dollars to the cent; a sub-cent call keeps four places rather than reading as free."""
    if amount != 0 and abs(amount) < Decimal("0.01"):
        return f"${amount:.4f}"
    return f"${amount:.2f}"


def _plural(count: int, word: str, plural: str) -> str:
    return f"{count} {word if count == 1 else plural}"


_RUN_KIND_WORDS: dict[str, str] = {
    "source_proposal": "find sources for {question}",
    "source_validation": "check {candidates} for a question",
    "weight_suggestion": "suggest points for {question}",
    "source_check": "check a registered source",
    "discovery": "run a saved search",
    "adhoc_url": "read a pasted document",
    "gap_regenerate": "look again at a coverage gap",
    "renewal_check": "check a protection credit before its review date",
}


def run_started_event(run: Run) -> tuple[str, dict[str, Any]]:
    """``Started: find sources for 'platforms'`` -- `` (attempt N)`` on a reclaimed run."""
    request = run.request if isinstance(run.request, dict) else {}
    question_key = request.get("question_key")
    question = f"'{question_key}'" if isinstance(question_key, str) else "a question"
    candidates = request.get("candidates")
    count = len(candidates) if isinstance(candidates, list) else 0
    template = _RUN_KIND_WORDS.get(run.kind, "run {kind}")
    what = template.format(
        question=question,
        candidates=_plural(count, "candidate source", "candidate sources"),
        kind=run.kind,
    )
    attempt = f" (attempt {run.attempts})" if run.attempts > 1 else ""
    return f"Started: {what}{attempt}", {"run_kind": run.kind, "attempt": run.attempts}


def run_finished_event(
    status: str, error_code: str | None, cost_usd: object, duration_ms: int
) -> tuple[str, dict[str, Any]]:
    """``Finished in 3m 40s · $0.23`` / ``Failed: <error_code>`` / ``Refused: <error_code>``.
    ``cost_usd`` is the run outcome's own decimal string."""
    cost: Decimal | None = None
    if isinstance(cost_usd, (str, int, Decimal)):
        try:
            cost = Decimal(str(cost_usd))
        except InvalidOperation:
            cost = None
    if status == "completed":
        text = f"Finished in {duration_words(duration_ms)}"
        if cost is not None:
            text += f" · {money_words(cost)}"
    elif status == "refused":
        text = f"Refused: {error_code}" if error_code else "Refused"
    else:
        text = f"Failed: {error_code}" if error_code else "Failed"
    return text, {
        "status": status,
        "error_code": error_code,
        "cost_usd": str(cost) if cost is not None else None,
        "duration_ms": duration_ms,
    }


def model_call_started_event(model: str, max_searches: int | None) -> tuple[str, dict[str, Any]]:
    """``Asked claude-sonnet-5 (up to 5 web searches)`` / ``Asked claude-opus-5-5``."""
    text = f"Asked {model}"
    if max_searches is not None:
        text += f" (up to {_plural(max_searches, 'web search', 'web searches')})"
    return text, {"model": model, "max_searches": max_searches}


def _field(obj: object, name: str) -> object:
    """A block field read off an SDK model or, defensively, a plain mapping."""
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _domain(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def search_results_event(content: object) -> tuple[str, dict[str, Any]]:
    """A ``web_search_tool_result`` block's content: a LIST is the results, an OBJECT is an error
    carrying its ``error_code`` (anthropic 1.8.0's ``WebSearchToolResultBlockContent``)."""
    if isinstance(content, list):
        results: list[dict[str, str]] = []
        for item in content:
            url = _field(item, "url")
            if not isinstance(url, str):
                continue
            title = _field(item, "title")
            title_text = title if isinstance(title, str) else ""
            results.append({"title": title_text[:MAX_RESULT_TITLE_CHARS], "url": url})
        domains: list[str] = []
        for result in results:
            domain = _domain(result["url"])
            if domain is not None and domain not in domains:
                domains.append(domain)
        count = len(results)
        if count == 0:
            text = "No results"
        else:
            shown = ", ".join(domains[:SEARCH_RESULT_DOMAINS_SHOWN])
            text = _plural(count, "result", "results") + (f": {shown}" if shown else "")
        return text, {
            "count": count,
            "results": results[:MAX_LOGGED_SEARCH_RESULTS],
            "error_code": None,
        }
    code = _field(content, "error_code")
    error_code = code if isinstance(code, str) and code else "unknown"
    return f"Search failed: {error_code}", {"count": 0, "results": [], "error_code": error_code}


_OUTCOME_WORDS: dict[str, str] = {
    "refusal": "Claude declined",
    "max_tokens": "cut off at the token limit",
    "unparseable": "the answer could not be read",
}


def model_call_finished_event(call: ModelCall[Any], elapsed_ms: int) -> tuple[str, dict[str, Any]]:
    """``Answered in 3m 12s · $0.23 · 2 searches``. ``elapsed_ms`` is the wall time of the whole
    metered call, a web search's ``pause_turn`` continuations included."""
    searches = call.usage.web_search_requests
    parts = [f"Answered in {duration_words(elapsed_ms)}", money_words(call.cost_usd)]
    if searches:
        parts.append(_plural(searches, "search", "searches"))
    if call.outcome in _OUTCOME_WORDS:
        parts.append(_OUTCOME_WORDS[call.outcome])
    return " · ".join(parts), {
        "model": call.answered_by,
        "stop_reason": call.stop_reason,
        "input_tokens": call.usage.input_tokens,
        "output_tokens": call.usage.output_tokens,
        "web_search_requests": searches,
        "cost_usd": str(call.cost_usd),
        "latency_ms": elapsed_ms,
    }


_HTTP_WORDS: dict[int, str] = {
    400: "bad request",
    401: "not authenticated",
    403: "permission denied",
    404: "not found",
    409: "conflict",
    413: "request too large",
    422: "request not accepted",
    429: "rate limited",
    500: "server error",
    529: "overloaded",
}


def _failure_words(status: str, http_status: int | None) -> str:
    if status == "timeout":
        return "timed out"
    if status == "rate_limited":
        return "rate limited (429)"
    if http_status is None:
        return "could not reach the API"
    if http_status < 400:
        return "the stream reported an error"
    words = _HTTP_WORDS.get(http_status, "server error" if http_status >= 500 else "refused")
    return f"{words} ({http_status})"


def model_call_failed_event(exc: ModelUnavailable) -> tuple[str, dict[str, Any]]:
    """``Claude call failed: permission denied (403) — <the API's own message>``."""
    text = f"Claude call failed: {_failure_words(exc.status, exc.http_status)}"
    if exc.message:
        text += f" — {exc.message}"
    return text, {
        "status": exc.status,
        "error_type": exc.error_type,
        "http_status": exc.http_status,
        "message": exc.message,
    }


_SKIP_WORDS: dict[str, str] = {
    "provider_disabled": "Not sent: the provider is switched off",
    "breaker_open": "Not sent: the breaker is open",
    "budget_exceeded": "Not sent: today's budget is spent",
    "budget_unset": "Not sent: no daily budget is set",
}


def model_call_skipped_event(reason: str) -> tuple[str, dict[str, Any]]:
    return _SKIP_WORDS.get(reason, f"Not sent: {reason}"), {"reason": reason}


# ── what the model seam and metering call ──────────────────────────────────────────────────────


async def note(kind: RunEventKind, event: tuple[str, dict[str, Any]]) -> None:
    """Append to the bound log, if any. Never raises."""
    run_log = current_run_log.get()
    if run_log is not None:
        await run_log.append(kind, *event)


async def note_skipped(reason: str) -> None:
    await note("model_call_skipped", model_call_skipped_event(reason))


async def note_failed(exc: ModelUnavailable) -> None:
    await note("model_call_failed", model_call_failed_event(exc))


async def note_finished(call: ModelCall[Any], elapsed_ms: int) -> None:
    await note("model_call_finished", model_call_finished_event(call, elapsed_ms))


async def note_model_call_started(model: str, max_searches: int | None) -> None:
    await note("model_call_started", model_call_started_event(model, max_searches))


async def note_continuing(pause_turns: int) -> None:
    await note("continuing", (CONTINUING_TEXT, {"pause_turns": pause_turns}))


class StreamTranslator:
    """Turns ONE streamed model call's events into rows (spec §3.4), against anthropic 1.8.0's
    ``AsyncMessageStream`` events (``lib/streaming/_messages.py``'s ``build_events``):

    - ``content_block_start`` of a ``thinking`` block opens ONE ``thinking`` row; the ``thinking``
      events' accumulated ``snapshot`` rewrites it at most every ``THINKING_UPDATE_SECONDS``, and
      the block's ``content_block_stop`` writes its final text;
    - ``content_block_start`` of the first ``text`` block writes ``writing``, once per call;
    - ``content_block_stop`` carries the ACCUMULATED block: a ``server_tool_use`` named
      ``web_search`` has its complete ``input.query`` only there (it streams as
      ``input_json_delta``), so it is written as ``search`` then; a ``web_search_tool_result``
      becomes ``search_results``.

    Every other event is ignored. ``feed`` never raises: an event of an unexpected shape is logged
    by type and skipped, and the call carries on.
    """

    def __init__(self, run_log: RunLog, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._log = run_log
        self._clock = clock
        self._thinking_seq: int | None = None
        self._thinking_text = ""
        self._thinking_written_at = 0.0
        self._writing = False

    async def feed(self, event: object) -> None:
        try:
            await self._feed(event)
        except Exception:
            log.warning(
                "intel.run_log_event_unreadable",
                run_id=str(self._log.run_id),
                event_type=str(_field(event, "type")),
            )

    async def _feed(self, event: object) -> None:
        event_type = _field(event, "type")
        if event_type == "content_block_start":
            await self._block_started(_field(event, "content_block"))
        elif event_type == "thinking":
            snapshot = _field(event, "snapshot")
            if isinstance(snapshot, str):
                self._thinking_text = snapshot
            if (
                self._thinking_seq is not None
                and self._clock() - self._thinking_written_at >= THINKING_UPDATE_SECONDS
            ):
                await self._log.update(
                    self._thinking_seq, self._thinking_text, {"chars": len(self._thinking_text)}
                )
                self._thinking_written_at = self._clock()
        elif event_type == "content_block_stop":
            await self._block_stopped(_field(event, "content_block"))

    async def _block_started(self, block: object) -> None:
        block_type = _field(block, "type")
        if block_type == "thinking":
            text = _field(block, "thinking")
            self._thinking_text = text if isinstance(text, str) else ""
            self._thinking_seq = await self._log.append(
                "thinking", self._thinking_text, {"chars": len(self._thinking_text)}
            )
            self._thinking_written_at = self._clock()
        elif block_type == "text" and not self._writing:
            self._writing = True
            await self._log.append("writing", WRITING_TEXT, {})

    async def _block_stopped(self, block: object) -> None:
        block_type = _field(block, "type")
        if block_type == "thinking":
            text = _field(block, "thinking")
            final = text if isinstance(text, str) else self._thinking_text
            if self._thinking_seq is not None:
                await self._log.update(self._thinking_seq, final, {"chars": len(final)})
            self._thinking_seq = None
            self._thinking_text = ""
        elif block_type == "server_tool_use" and _field(block, "name") == "web_search":
            query = _field(_field(block, "input"), "query")
            query_text = query if isinstance(query, str) else ""
            await self._log.append("search", f"Searched: {query_text}", {"query": query_text})
        elif block_type == "web_search_tool_result":
            await self._log.append(
                "search_results", *search_results_event(_field(block, "content"))
            )
