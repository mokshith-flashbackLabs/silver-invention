"""INVARIANTS #50: a web-only claim needs corroboration. ONE predicate, for every caller: the
approvable flag on both reads, the in-transaction re-check on decision, and (step 5) the
per-option ``corroborated`` flag on weight suggestions.

*Amended 2026-10-04 (spec 2026-10-04-intel-evidence-quality §4).* Corroboration counts
INDEPENDENT source groups, not distinct domains. Two collapses come first:

- **Echoes.** Signals whose verbatim excerpts share a long run of the same words, or most of the
  shorter excerpt's words (``ECHO_*`` in intel/bounds.py), are one group, transitively: four
  outlets quoting one sentence of a company's announcement are one source, not four.
- **A company's own domains** (``PUBLISHER_ORGANISATIONS``) are one publisher: google.com and
  blog.youtube corroborate nothing about YouTube to each other.

A claim is corroborated by ``CORROBORATION_MIN_PUBLISHERS`` such groups, or by a ``listed``
signal on its own (a page from a source an operator registered or pasted). The echo machinery is
shared with the cross-kind overlap check (intel/overlap.py), so "the same words" means one thing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from imageshield.intel.bounds import (
    CORROBORATION_MIN_PUBLISHERS,
    ECHO_MIN_OVERLAP_WORDS,
    ECHO_MIN_SHARED_RUN_WORDS,
    ECHO_MIN_TOKEN_OVERLAP,
    PUBLISHER_ORGANISATIONS,
)
from imageshield.intel.proposal_models import ContextSignal

# Apostrophes and backticks are deleted (a straight and a curly apostrophe give one word); every
# other non-word character, quote marks included, separates words. Built from code points so the
# curly marks stay legible in an ASCII source file: ' ` U+2018 U+2019 U+201B U+02BC U+00B4.
_APOSTROPHE_CODES = (0x27, 0x60, 0x2018, 0x2019, 0x201B, 0x02BC, 0x00B4)
_APOSTROPHES = re.compile("[" + re.escape("".join(map(chr, _APOSTROPHE_CODES))) + "]")
_WORD = re.compile(r"\w+")
_ORGANISATION_OF: dict[str, str] = {
    domain: f"org:{organisation}"
    for organisation, domains in PUBLISHER_ORGANISATIONS.items()
    for domain in domains
}


def organisation(publisher_domain: str) -> str:
    """The publisher a domain speaks for: its organisation when it is one of a company's own
    domains, else the registrable domain itself."""
    domain = publisher_domain.lower()
    return _ORGANISATION_OF.get(domain, domain)


@dataclass(frozen=True)
class ExcerptText:
    """One excerpt, normalised: its distinct words and its runs of ``ECHO_MIN_SHARED_RUN_WORDS``
    consecutive words."""

    words: frozenset[str]
    runs: frozenset[tuple[str, ...]]


def excerpt_text(quote: str) -> ExcerptText:
    tokens = _WORD.findall(_APOSTROPHES.sub("", quote.casefold()))
    n = ECHO_MIN_SHARED_RUN_WORDS
    runs = frozenset(tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1))
    return ExcerptText(frozenset(tokens), runs)


def excerpts_echo(a: ExcerptText, b: ExcerptText) -> bool:
    """One excerpt repeats the other: a shared run of words, or most of the shorter's words."""
    if a.runs & b.runs:
        return True
    short, other = (a, b) if len(a.words) <= len(b.words) else (b, a)
    if len(short.words) < ECHO_MIN_OVERLAP_WORDS:
        return False
    return len(short.words & other.words) >= ECHO_MIN_TOKEN_OVERLAP * len(short.words)


class UnionFind:
    def __init__(self) -> None:
        self._parent: dict[Hashable, Hashable] = {}

    def find(self, item: Hashable) -> Hashable:
        root = self._parent.setdefault(item, item)
        while self._parent[root] != root:
            root = self._parent[root]
        while item != root:  # path compression, iteratively: no recursion limit on a long chain
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: Hashable, b: Hashable) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_b] = root_a


def unite_echoes(groups: UnionFind, items: Iterable[tuple[Hashable, ExcerptText]]) -> None:
    """Join every two items whose excerpts echo. Shared runs through an index; the overlap rule
    pairwise, only between excerpts long enough for it and not yet in one group."""
    eligible: list[tuple[Hashable, ExcerptText]] = []
    first_with_run: dict[tuple[str, ...], Hashable] = {}
    for key, text in items:
        groups.find(key)
        for run in text.runs:
            first = first_with_run.setdefault(run, key)
            if first != key:
                groups.union(first, key)
        if len(text.words) >= ECHO_MIN_OVERLAP_WORDS:
            eligible.append((key, text))
    for i, (key_a, text_a) in enumerate(eligible):
        for key_b, text_b in eligible[i + 1 :]:
            if groups.find(key_a) != groups.find(key_b) and excerpts_echo(text_a, text_b):
                groups.union(key_a, key_b)


def source_groups(
    signals: Sequence[ContextSignal], *, key: Callable[[ContextSignal], Hashable]
) -> dict[UUID, Hashable]:
    """Each signal's group: signals sharing ``key`` are one group, and so are signals whose
    excerpts echo (transitively). Returns signal id -> group root."""
    groups = UnionFind()
    owner: dict[Hashable, UUID] = {}
    for signal in signals:
        groups.union(("signal", signal.signal_id), ("key", key(signal)))
        owner[("signal", signal.signal_id)] = signal.signal_id
    unite_echoes(
        groups,
        (
            (("signal", signal.signal_id), excerpt_text(quote))
            for signal in signals
            for quote in signal.excerpts
        ),
    )
    return {signal.signal_id: groups.find(("signal", signal.signal_id)) for signal in signals}


def independent_sources(signals: Sequence[ContextSignal]) -> int:
    """How many independent sources the ACTIVE signals come from: one per organisation (or
    registrable domain), with every echo collapsed into one."""
    active = [s for s in signals if s.status == "active"]
    return len(set(source_groups(active, key=lambda s: organisation(s.publisher_domain)).values()))


def uncorroborated(signals: Sequence[ContextSignal]) -> bool:
    """True when the active signals are all ``trust = web`` and come from fewer than
    ``CORROBORATION_MIN_PUBLISHERS`` independent sources. ``publisher_domain`` is the
    registrable domain stored at fetch time (intel/publisher.py), so two subdomains of one
    publisher are one publisher; a company's own domains are one organisation; and echoes of one
    text are one source. A listed signal corroborates on its own."""
    active = [s for s in signals if s.status == "active"]
    if any(s.trust == "listed" for s in active):
        return False
    return independent_sources(active) < CORROBORATION_MIN_PUBLISHERS
