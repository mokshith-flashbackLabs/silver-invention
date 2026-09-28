"""Model prices (spec §5). PER MILLION TOKENS, divided in code: a per-token string
such as "0.000003" is phone-shaped and fails the build gate. An unknown model
REFUSES the call (fail closed) -- changing the model is a deliberate price change.

Every rate below is the 5-minute (ephemeral, default) cache-write price from the
step 0 findings (docs/superpowers/specs/2026-09-28-likeness-intel-step0-findings.md),
quoted from https://platform.claude.com/docs/en/about-claude/pricing. The 1-hour
cache-write rate is opt-in and this repo never asks for it, so it is not priced.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

_MILLION = Decimal(1_000_000)
_THOUSAND = Decimal(1_000)

# (input, output, cache_write, cache_read) USD per MTok -- step 0 findings.
PRICES_PER_MTOK: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {
    "claude-sonnet-5": (Decimal("2"), Decimal("10"), Decimal("2.50"), Decimal("0.20")),
    "claude-opus-5-5": (Decimal("4"), Decimal("20"), Decimal("5"), Decimal("0.20")),
}
WEB_SEARCH_PER_THOUSAND = Decimal("10")


class UnknownModelPrice(Exception):
    pass


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    web_search_requests: int


def cost_of(model_id: str, usage: Usage) -> Decimal:
    try:
        rin, rout, rcw, rcr = PRICES_PER_MTOK[model_id]
    except KeyError as exc:
        raise UnknownModelPrice(model_id) from exc
    tokens_cost = (
        usage.input_tokens * rin
        + usage.output_tokens * rout
        + usage.cache_creation_input_tokens * rcw
        + usage.cache_read_input_tokens * rcr
    )
    return tokens_cost / _MILLION + usage.web_search_requests * WEB_SEARCH_PER_THOUSAND / _THOUSAND
