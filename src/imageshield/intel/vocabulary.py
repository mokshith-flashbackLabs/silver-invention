"""The backend's scoring vocabulary, read once per use (spec §3.1, §3.8, §4.9).

``intel_vocabulary.document`` holds the backend's push verbatim. This module reads it into
typed, immutable values: questions with deductions and caps, the tag registry, the
option-to-tag map for the live quiz, and the ordered rename log. So every caller (generation,
decisions, the reconcile, the reads) answers "is this cell live, and what is it worth" the same
way.

A question is ``mutable`` exactly when the backend's scoring ``type`` is ``'mutable'``: the
push carries ``type``, not a separate flag (spec note, 2026-09-30).

A stored document this module cannot read parses to ``None`` and is logged. Every caller then
behaves as with no vocabulary at all: no weight proposal, no approval. That is the direction a
missing input must fail in.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import structlog

from imageshield.intel.models import Vocabulary
from imageshield.intel.tags import TagRegistry

log = structlog.get_logger("imageshield.intel")

_NON_ALPHANUMERIC_RUN = re.compile(r"[\W_]+")


def normalise_subject(text: str) -> str:
    """spec §4.9: exact on lowercase text with non-alphanumerics collapsed. Nothing is fuzzy:
    "X (Twitter)" and "x_twitter" meet; "Bumble" and "Bumbl" never do."""
    return _NON_ALPHANUMERIC_RUN.sub(" ", text.casefold()).strip()


@dataclass(frozen=True)
class VocabQuestion:
    key: str
    prompt: str
    type: str | None
    options: tuple[str, ...]
    deductions: Mapping[str, int]
    cap: int | None

    @property
    def mutable(self) -> bool:
        return self.type == "mutable"


@dataclass(frozen=True)
class Rename:
    release_no: int
    question_key: str
    old_option: str
    new_option: str


@dataclass(frozen=True)
class RegistryEntry:
    slug: str
    label: str
    description: str
    retired: bool


@dataclass(frozen=True)
class ScoringVocabulary:
    release_no: int
    map_version: int
    scoring_version: str
    questions: Mapping[str, VocabQuestion]
    tags: Mapping[str, RegistryEntry]
    option_tags: Mapping[tuple[str, str], tuple[str, ...]]
    renamed: tuple[Rename, ...]
    mapped_tags: frozenset[str]

    def question(self, key: str) -> VocabQuestion | None:
        return self.questions.get(key)

    def deduction(self, question_key: str, option: str) -> int | None:
        """The live deduction of one cell. None when the question, the option or its
        deduction is absent: an option the engine cannot price is not a live cell."""
        question = self.questions.get(question_key)
        if question is None or option not in question.options:
            return None
        return question.deductions.get(option)

    def registry(self) -> TagRegistry:
        return TagRegistry(
            active=frozenset(s for s, e in self.tags.items() if not e.retired),
            retired=frozenset(s for s, e in self.tags.items() if e.retired),
        )

    def fold_renames(self, question_key: str, option: str, *, after_release_no: int) -> str:
        """Apply every rename of ``question_key`` above ``after_release_no`` to ``option``,
        in ``release_no`` order and chained: A -> B at 5 and B -> C at 6 lands A on C.

        Within ONE release the entries apply simultaneously, as one mapping, so a swap (A -> B
        and B -> A in the same release) moves A to B rather than back to A."""
        by_release: dict[int, dict[str, str]] = {}
        for rename in self.renamed:
            if rename.question_key == question_key and rename.release_no > after_release_no:
                by_release.setdefault(rename.release_no, {})[rename.old_option] = rename.new_option
        current = option
        for release_no in sorted(by_release):
            current = by_release[release_no].get(current, current)
        return current

    def renamed_away(self, question_key: str, option: str, *, after_release_no: int) -> bool:
        return any(
            r.question_key == question_key
            and r.old_option == option
            and r.release_no > after_release_no
            for r in self.renamed
        )

    def subject_is_mapped(self, subject_key: str) -> bool:
        """Whether a normalised subject already names a MAPPED tag, by slug or label. The quiz
        covers such a subject, so it is never a coverage gap."""
        for slug in self.mapped_tags:
            entry = self.tags.get(slug)
            label = entry.label if entry is not None else slug
            if subject_key in (normalise_subject(slug), normalise_subject(label)):
                return True
        return False


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("not an integer")
    return value


def _str(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("not a string")
    return value


def parse_vocabulary(row: Vocabulary) -> ScoringVocabulary | None:
    document = row.document
    try:
        questions: dict[str, VocabQuestion] = {}
        for raw in document.get("questions") or []:
            deductions = raw.get("deductions") or {}
            cap = raw.get("cap")
            question = VocabQuestion(
                key=_str(raw["key"]),
                prompt=_str(raw.get("prompt", "")),
                type=_str(raw["type"]) if raw.get("type") is not None else None,
                options=tuple(_str(o) for o in raw["options"]),
                deductions={_str(k): _int(v) for k, v in deductions.items()},
                cap=_int(cap) if cap is not None else None,
            )
            questions[question.key] = question
        tags: dict[str, RegistryEntry] = {}
        for raw in document.get("tags") or []:
            slug = _str(raw["slug"])
            tags[slug] = RegistryEntry(
                slug=slug,
                label=_str(raw.get("label", slug)),
                description=_str(raw.get("description", "")),
                retired=bool(raw.get("retired", False)),
            )
        option_tags: dict[tuple[str, str], tuple[str, ...]] = {}
        for raw in document.get("option_tags") or []:
            option_tags[(_str(raw["question_key"]), _str(raw["option"]))] = tuple(
                _str(t) for t in raw.get("tags") or []
            )
        renamed = tuple(
            Rename(
                release_no=_int(r["release_no"]),
                question_key=_str(r["question_key"]),
                old_option=_str(r["old_option"]),
                new_option=_str(r["new_option"]),
            )
            for r in document.get("renamed") or []
        )
    except (KeyError, TypeError, AttributeError) as exc:
        log.error(
            "intel.vocabulary_unreadable",
            release_no=row.release_no,
            map_version=row.map_version,
            error=type(exc).__name__,
        )
        return None
    return ScoringVocabulary(
        release_no=row.release_no,
        map_version=row.map_version,
        scoring_version=row.scoring_version,
        questions=questions,
        tags=tags,
        option_tags=option_tags,
        renamed=renamed,
        mapped_tags=frozenset(t for ts in option_tags.values() for t in ts),
    )
