"""The calibration harness.

    calibrate observe   --provider hive --eval-set v1 --confirm
    calibrate sweep     --provider hive --eval-set v1
    calibrate propose   --provider hive --eval-set v1 --version v2
    calibrate replay    --config <id>
    calibrate activate  --config <id> --confirm --by <name>
    calibrate trust     --provider hive --confirm --by <name> --reason <text>

Devtools, not a deployable: it holds no HTTP surface and is never imported by
the service. It does use the production engine — ``imageshield.calibration``
and the real ``SearchProvider`` adapters — because a calibration measured
against a reimplementation measures the reimplementation.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, cast

import httpx

from imageshield.calibration.store import PostgresCalibrationStore
from imageshield.config import Config, load_config
from imageshield.db.connection import make_async_pool
from imageshield.search.google import GoogleWebDetectionProvider
from imageshield.search.hive import HiveWebSearchProvider
from imageshield.search.provider import SearchProvider
from imageshield.search.store import fan_out
from imageshield.search.urlhash import url_hash
from imageshield.types import ProviderId, parse_provider_id


def build_provider(
    provider_id: ProviderId, config: Config, client: httpx.AsyncClient
) -> SearchProvider:
    """The same adapters the worker constructs (``search/worker.py``'s
    ``build_providers``). Adding a provider here without adding it there
    would calibrate something we do not run."""
    if provider_id == "hive":
        return HiveWebSearchProvider(
            base_url=config.hive_base_url,
            api_key=config.hive_api_key,
            timeout_seconds=config.provider_timeout_seconds,
            client=client,
        )
    if provider_id == "google":
        return GoogleWebDetectionProvider(
            endpoint=config.google_vision_endpoint,
            api_key=config.google_vision_api_key,
            timeout_seconds=config.provider_timeout_seconds,
            client=client,
        )
    raise SystemExit(f"no adapter for provider {provider_id!r}")


async def observe_seed(
    store: PostgresCalibrationStore,
    provider: SearchProvider,
    eval_set_id: str,
    seed_uri: str,
) -> int:
    """Call the provider once for one seed; write an observation for every
    labelled candidate it returned, and a coverage row either way.

    Candidate matching goes through the production ``fan_out`` + ``url_hash``.
    If the eval matcher normalised URLs differently from the dedup key, the
    measurement would disagree with the system being measured.

    Returns the number of observations written.
    """
    result = await provider.search(seed_uri)
    await store.record_seed_coverage(
        eval_set_id, seed_uri, provider.id, result.status, len(result.matches)
    )
    if result.status != "ok":
        # We learned nothing about this seed. No coverage means its items'
        # absences are correctly excluded from the recall denominator.
        return 0

    returned = {key.url_hash: key for key in fan_out(result.matches)}
    written = 0
    for item_id, candidate_url in await store.eval_items_for_seed(
        eval_set_id, seed_uri
    ):
        key = returned.get(url_hash(candidate_url))
        if key is None:
            continue
        await store.upsert_eval_observation(
            item_id,
            provider.id,
            provider.score_kind,
            key.match.provider_score,
            key.match.provider_category,
            key.match.query_quality,
            provider.score_version,
        )
        written += 1
    return written


async def run_observe(args: argparse.Namespace) -> int:
    config = load_config()
    provider_id = parse_provider_id(args.provider)
    pool = make_async_pool(config.database_url, min_size=1, max_size=2)
    await pool.open()
    try:
        async with httpx.AsyncClient() as client:
            store = PostgresCalibrationStore(pool)
            provider = build_provider(provider_id, config, client)
            seeds = await store.eval_seeds(args.eval_set)
            if not seeds:
                print(f"eval set {args.eval_set!r} has no items — nothing to observe")
                return 1
            # Spends real provider money. Say how much before doing it.
            print(f"{len(seeds)} seed(s) x 1 call to {provider_id} = {len(seeds)} calls")
            if not args.confirm:
                print("refusing without --confirm (this spends provider budget)")
                return 1
            total = 0
            for seed_uri in seeds:
                written = await observe_seed(store, provider, args.eval_set, seed_uri)
                total += written
                print(f"  {seed_uri}: {written} observation(s)")
            uncovered = await store.uncovered_seeds(args.eval_set, provider_id)
            print(f"{total} observation(s) written; {len(uncovered)} seed(s) uncovered")
            if uncovered:
                print("  uncovered (provider call did not succeed):")
                for seed in uncovered:
                    print(f"    {seed}")
    finally:
        await pool.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calibrate")
    sub = parser.add_subparsers(dest="command", required=True)

    observe = sub.add_parser(
        "observe", help="call the real provider over an eval set's seeds"
    )
    observe.add_argument("--provider", required=True)
    observe.add_argument("--eval-set", required=True)
    observe.add_argument(
        "--confirm", action="store_true", help="required: this spends provider budget"
    )
    observe.set_defaults(func=run_observe)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func = cast(
        "Callable[[argparse.Namespace], Coroutine[Any, Any, int]]", args.func
    )
    return asyncio.run(func(args))


if __name__ == "__main__":
    sys.exit(main())
