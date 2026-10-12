"""The score page's words (spec 2026-10-11-intel-score-insights §4.1-4.2): the word list and the
figure check that every drafted risk-factor line, general recommendation and threat card line must
pass. The backend runs the same rules again on the words an operator applies
(image_backend src/insights/copy.ts). The two lists must match, and each repo pins its own copy
with a test."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

CopyRule = Literal["reassurance", "finding", "answer", "spelled_figure", "address"]

WORD_RULES: tuple[tuple[CopyRule, re.Pattern[str]], ...] = (
    ("reassurance", re.compile(r"\bsafe\b|\ball[- ]clear\b|\bguarantee(?:d|s)?\b", re.IGNORECASE)),
    (
        "finding",
        re.compile(
            r"\bfound\b|\bdetected\b|\bmatch(?:es|ed)?\b|\bhits?\b"
            r"|\byour (?:photo|image|picture)s?\b",
            re.IGNORECASE,
        ),
    ),
    ("answer", re.compile(r"\byou answered\b|\byour answer\b", re.IGNORECASE)),
    # A figure written in words cannot be checked against a quote, so it is refused (final review
    # I5, 2026-10-12): "nine in ten", "half of", "twice as". "two-factor" stays legal.
    (
        "spelled_figure",
        re.compile(
            r"\bper ?cent\b"
            r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:in|out of)\s+"
            r"(?:two|three|four|five|six|seven|eight|nine|ten|\d+)\b"
            r"|\b(?:half|a quarter|a third|three quarters|two thirds)\s+of\b"
            r"|\btwice as\b",
            re.IGNORECASE,
        ),
    ),
    (
        "address",
        re.compile(
            r"https?:|\bwww\.|\b[a-z0-9-]+\.[a-z]{2,}\b|\S+@\S+"
            r"|\+\d[\d ()-]{6,}\d|\d{10,}|«masked»",
            re.IGNORECASE,
        ),
    ),
)

_FIGURE = re.compile(r"(\d+(?:[.,]\d+)*)(\s*(?:%|per ?cent\b))?", re.IGNORECASE)


@dataclass(frozen=True)
class Figure:
    raw: str
    core: str
    percent: bool


def copy_problems(text: str) -> list[CopyRule]:
    return [rule for rule, pattern in WORD_RULES if pattern.search(text)]


def figures(text: str) -> list[Figure]:
    return [
        Figure(m.group(0).strip(), m.group(1).replace(",", ""), m.group(2) is not None)
        for m in _FIGURE.finditer(text)
    ]


def unquoted_figures(text: str, quotes: Iterable[str]) -> list[str]:
    """The figures in ``text`` that no quote carries. A figure with a percent needs a quoted figure
    with one; a bare figure matches either. Compared as whole tokens, never as substrings."""
    quoted = [f for quote in quotes for f in figures(quote)]
    return [
        f.raw
        for f in figures(text)
        if not any(q.core == f.core and (q.percent or not f.percent) for q in quoted)
    ]
