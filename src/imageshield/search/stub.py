"""The stub search provider — what `SEARCH_PROVIDER=stub` is wired to.

`Config.search_provider` shipped as a field with a docstring calling it "the
only thing standing between a test run and billable Hive traffic", and nothing
outside `config.py` read it. `build_providers()` constructed the real Hive and
Google adapters unconditionally; 0004 seeds both `providers.enabled = true`; both
have a NULL `daily_budget_usd`, which `providers/budget.py` permits. So a
`POST /v1/search` in development with the worker running spent real money against
real keys, and the switch that was supposed to prevent it was inert.

A safety switch wired to nothing is worse than no switch. It produces false
confidence, and the person relying on it is whoever read the docstring.

WHAT THIS ADAPTER IS ALLOWED TO DO. Nothing. It returns zero matches, opens no
socket, and holds no credential. It is not a fixture generator: a stub that
manufactured plausible matches would put fabricated findings on a real
`user_ref`'s report, and this product's entire output is "your face appeared
here".

WHY IT KEEPS ITS OWN provider_id. The shortcut is to register it under `hive` so
a dev run completes with `providers_succeeded = ['hive']`. That writes
attestations claiming a provider we never called, at a `score_version` we never
ran, and it defeats the one thing `raw_payload` and `score_version` exist for —
recalibrating history when a provider retunes (CLAUDE.md §7.2). It is `stub`, and
because `stub` has no `providers` row and no calibration config it has no policy
entry, so rule 1 of `calibration/bands.py` bands anything it ever produced as
`review` — never `auto_confirm`, never `drop` (§7.3).

WHY IT IS REFUSED IN PRODUCTION. See `Config._live_provider_in_production`. The
field defaults to 'stub', so production would inherit it from an unset variable
rather than from a decision, and the result is a deploy that answers "no matches
in monitored sources" for every user forever with nothing failing anywhere.

WHAT IT DOES NOT MAKE TRUE, on the record. The stub is registered as an adapter;
it is not registered as a *provider*. `search_runs.providers_attempted` comes
from `providers.enabled` in the database (`SearchStore.enabled_provider_ids`),
which lists `hive` and `google` and not `stub`. So under `SEARCH_PROVIDER=stub` a
dev run dispatches against provider ids that now have no adapter, and the runner
records each as `status='error'`, `error_detail='no adapter registered for this
provider'` — no network call, no attestation, no cadence change (invariant #42:
no provider succeeded, so the run is not evidence of an empty scan). That is the
honest outcome and it is deliberately not papered over: this adapter's job is to
make billable traffic impossible, not to make a dev run look successful. Giving
`stub` a `providers` row would put a fake provider in production's control plane
and is not worth it.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from imageshield.search.provider import ProviderResult
from imageshield.types import ProviderId

# Verbatim on every provider_calls row this adapter produces. Anyone
# recalibrating over history, or reading a row six months from now, needs the
# payload itself to say what it is — an empty `matches` list from a stub and an
# empty one from a real provider are the same three characters otherwise, and
# only one of them is evidence that we looked.
_RAW_RESPONSE: dict[str, Any] = {
    "stub": True,
    "provider": "stub",
    "matches": [],
    "note": (
        "SEARCH_PROVIDER=stub: no provider was called and nothing was searched."
        " This is NOT evidence of an empty scan."
    ),
}

_ERROR_DETAIL = (
    "stub provider: no search performed (SEARCH_PROVIDER=stub)"
)


class StubSearchProvider:
    """Zero matches, no network, deterministic."""

    id: ProviderId = ProviderId("stub")
    # Declared as image_search rather than face_search, and the choice is not
    # arbitrary. `kind` is a claim about coverage (§7.1: a deepfake is a novel
    # image, so image search returns nothing for it *silently*). Claiming
    # face_search here would assert coverage that neither this adapter nor the
    # real stack it stands in for provides — the exact silent-gap failure §7.1
    # exists to name. image_search is what Hive and Google both declare, so a
    # kind-aware orchestrator treats a stub run exactly as it treats the
    # image-search-only stack that is actually deployed.
    kind: Literal["image_search", "face_search", "classifier"] = "image_search"
    score_kind: Literal["numeric", "categorical"] = "numeric"
    # Never a real provider's version string. A row carrying
    # 'hive-web-search-v1' that Hive never produced is unrecalibratable and
    # indistinguishable from a real one.
    score_version = "stub-no-op-v1"

    async def search(
        self, seed_url: str, max_results: int | None = None
    ) -> ProviderResult:
        """Return an empty ``ok`` result without touching the network.

        ``seed_url`` and ``max_results`` are accepted to satisfy the protocol and
        deliberately unused — not even logged. The seed URL is a presigned GET
        into the proxy's S3 and carries a signature.
        """
        started = time.monotonic()
        return ProviderResult(
            provider_id=self.id,
            status="ok",
            matches=[],
            # A copy per call: raw_response lands in a Jsonb() adapter and a
            # shared mutable dict is one accidental mutation from rewriting
            # history on every row already stored.
            raw_response=dict(_RAW_RESPONSE),
            http_status=None,
            latency_ms=int(1000 * (time.monotonic() - started)),
            attempts=1,
            # error_detail on an 'ok' row is unusual and is the point: it is the
            # short, stable summary that outlives raw_response when the retention
            # job nulls the JSONB after RAW_RESPONSE_RETENTION_DAYS. Without it, a
            # months-old stub row becomes indistinguishable from a real scan that
            # found nothing.
            error_detail=_ERROR_DETAIL,
        )
