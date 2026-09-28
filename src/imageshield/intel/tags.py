"""Exposure tags are backend-owned data (spec §3.1). Shape is checked here and in
the DB (intel_tags_well_formed); membership against the loaded vocabulary."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

TAG_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def is_well_formed(slug: str) -> bool:
    return TAG_SLUG_RE.fullmatch(slug) is not None


@dataclass(frozen=True)
class TagRegistry:
    active: frozenset[str]
    retired: frozenset[str]


def membership_problems(added: Sequence[str], registry: TagRegistry) -> tuple[list[str], list[str]]:
    """(unknown, retired) among tags a write ADDS. Retired tags already on a
    proposal's own target are not 'added' and never reach this check (spec §3.1).

    ``added`` is a Sequence, not an Iterable: a generator would be exhausted by
    the first comprehension below, silently losing every retired tag on the
    second pass.
    """
    unknown = [t for t in added if t not in registry.active and t not in registry.retired]
    retired = [t for t in added if t in registry.retired]
    return unknown, retired
