"""The daily news watch (spec 2026-10-09-intel-news-watch-design §2.2). Pure: no database, no
model.

Stage 1's sources are evidence for POINTS -- research, statistics, policies -- read weekly, so
nothing went looking for current incidents, and a dev rerun with 222 risk-raising incidents in hand
proposed no threat: 193 of them were older than the threat window. A threat needs fresh news.

One saved search per registry tag of kind ``platform`` that a live quiz option maps, read every
NEWS_WATCH_CHECK_EVERY_HOURS. Code creates it, never the model, and its query names only the
platform and generic kinds of likeness misuse -- never a case, a product or a person -- so what it
finds is the search's finding, not ours (owner, 2026-10-09: no bias from our side). The worker's
existing unmapped pause follows the quiz: a watch whose tag stops being mapped is paused, and
resumed when it is mapped again. An operator may disable or edit a watch like any source; a
disabled one is never re-created, because the row still exists.
"""

from __future__ import annotations

from dataclasses import dataclass

from imageshield.intel.vocabulary import ScoringVocabulary

NEWS_WATCH_ORIGIN = "news_watch"
NEWS_WATCH_CREATED_BY = "system:news_watch"
NEWS_WATCH_CHECK_EVERY_HOURS = 24
# Generic kinds of likeness misuse, the same for every platform.
_MISUSE_TERMS = "deepfake OR impersonation OR leaked photos OR sextortion"


@dataclass(frozen=True)
class Watch:
    tag: str
    query_text: str


def watched_tags(vocabulary: ScoringVocabulary) -> list[str]:
    """Every mapped, unretired registry tag of kind ``platform``, in slug order."""
    return sorted(
        slug
        for slug in vocabulary.mapped_tags
        if (entry := vocabulary.tags.get(slug)) is not None
        and entry.kind == "platform"
        and not entry.retired
    )


def watches(vocabulary: ScoringVocabulary) -> list[Watch]:
    """The watch each watched tag should have: its query names the platform by its label."""
    return [
        Watch(tag=slug, query_text=f"{vocabulary.tags[slug].label} {_MISUSE_TERMS}")
        for slug in watched_tags(vocabulary)
    ]
