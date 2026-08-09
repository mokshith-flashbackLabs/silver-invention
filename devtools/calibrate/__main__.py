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
import json
import sys
from collections.abc import Callable, Coroutine, Sequence
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import httpx

from imageshield.calibration.bands import validate_numeric_bands
from imageshield.calibration.metrics import (
    CategoricalSweep,
    EvalRow,
    NumericSweep,
    effective_sample_size,
    sweep_categorical,
    sweep_numeric,
)
from imageshield.calibration.models import Band
from imageshield.calibration.report import render_categorical_sweep, render_numeric_sweep
from imageshield.calibration.store import (
    PostgresCalibrationStore,
    ProviderMeta,
    parse_bands,
)
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
    if result.status != "ok":
        # We learned nothing about this seed. Coverage is written here, as a
        # failing status, so its items' absences are correctly excluded from
        # the recall denominator (uncovered_seeds sees this row and excludes
        # them) rather than silently counted as misses.
        await store.record_seed_coverage(
            eval_set_id, seed_uri, provider.id, result.status, len(result.matches)
        )
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
    # The 'ok' coverage row is written only after the loop completes, not
    # before it. Writing it up front would mean a crash partway through the
    # loop leaves status='ok' with some observations missing — and a missing
    # observation under an 'ok' coverage row reads as a provider MISS
    # forever, deflating recall in exactly the direction this table exists
    # to prevent.
    await store.record_seed_coverage(
        eval_set_id, seed_uri, provider.id, result.status, len(result.matches)
    )
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


def build_bands_json(drop_max: Decimal, auto_min: Decimal) -> list[dict[str, str]]:
    """The three-band numeric shape, in the provider's NATIVE units."""
    return [
        {"band": "drop", "max": str(drop_max)},
        {"band": "review", "min": str(drop_max), "max": str(auto_min)},
        {"band": "auto_confirm", "min": str(auto_min)},
    ]


async def load_sweep(
    store: PostgresCalibrationStore, provider_id: ProviderId, eval_set_id: str
) -> tuple[
    ProviderMeta,
    tuple[EvalRow, ...],
    tuple[str, ...],
    NumericSweep | CategoricalSweep,
]:
    meta = await store.provider_meta(provider_id)
    if meta is None:
        raise ValueError(f"unknown provider {provider_id!r}")
    rows = await store.eval_rows(eval_set_id, provider_id)
    if not rows:
        raise ValueError(f"eval set {eval_set_id!r} has no items")
    uncovered = await store.uncovered_seeds(eval_set_id, provider_id)
    sweep: NumericSweep | CategoricalSweep = (
        sweep_numeric(rows, meta.score_domain)
        if meta.score_kind == "numeric"
        else sweep_categorical(rows, meta.score_domain.categories or ())
    )
    return meta, rows, uncovered, sweep


async def run_sweep(args: argparse.Namespace) -> int:
    """Writes nothing. Ever."""
    config = load_config()
    provider_id = parse_provider_id(args.provider)
    async with make_async_pool(config.database_url, min_size=1, max_size=2) as pool:
        store = PostgresCalibrationStore(pool)
        _meta, _rows, uncovered, sweep = await load_sweep(
            store, provider_id, args.eval_set
        )
        if isinstance(sweep, NumericSweep):
            print(render_numeric_sweep(sweep, provider_id, args.eval_set, uncovered))
        else:
            print(render_categorical_sweep(sweep, provider_id, args.eval_set, uncovered))
    return 0


def _band_precision(sweep: NumericSweep | CategoricalSweep, band: Band) -> float | None:
    """The recommended boundary's precision, read off ``sweep.points``.

    ``None`` when there is no recommendation for this band, or when the
    sweep is categorical — a mapping of many categories has no single
    boundary to report a figure for, so this stays honest rather than
    inventing one.
    """
    if not isinstance(sweep, NumericSweep):
        return None
    if band != "auto_confirm" or sweep.recommended_auto_confirm_min is None:
        return None
    for point in sweep.points:
        if point.threshold == sweep.recommended_auto_confirm_min:
            return point.precision_at_or_above.value
    return None


def _band_npv(sweep: NumericSweep | CategoricalSweep) -> float | None:
    """The recommended drop boundary's NPV, read off ``sweep.points``.

    ``None`` when there is no drop recommendation, or the sweep is
    categorical (same reasoning as ``_band_precision``).
    """
    if not isinstance(sweep, NumericSweep):
        return None
    if sweep.recommended_drop_max is None:
        return None
    for point in sweep.points:
        if point.threshold == sweep.recommended_drop_max:
            return point.npv_below.value
    return None


async def propose_config(
    store: PostgresCalibrationStore,
    provider_id: ProviderId,
    eval_set_id: str,
    version: str,
    bands_json: object | None,
) -> UUID:
    """Write an INACTIVE config. Raises ValueError rather than writing
    something the data does not support."""
    meta, rows, _uncovered, sweep = await load_sweep(store, provider_id, eval_set_id)
    # Whether the caller handed us bands directly, as opposed to letting the
    # sweep derive them. `measured` below must not attach a figure measured
    # at the sweep's recommended boundary to a config built from a different,
    # hand-supplied one.
    bands_were_supplied = bands_json is not None

    if bands_json is None:
        # Narrow on the sweep type, not on meta.score_kind — mypy strict does
        # not learn the union member from a string comparison on another
        # object, and an isinstance check here is also the honest guard if
        # the two ever disagree.
        if isinstance(sweep, NumericSweep):
            if (
                sweep.recommended_auto_confirm_min is None
                or sweep.recommended_drop_max is None
            ):
                raise ValueError(
                    "sweep produced no recommendation on this set — refusing to "
                    "invent boundaries. Report the gap; do not loosen the target."
                )
            bands_json = build_bands_json(
                sweep.recommended_drop_max, sweep.recommended_auto_confirm_min
            )
        else:
            # Categorical refuses on a DIFFERENTLY-SHAPED condition than
            # numeric, deliberately: numeric refuses unless BOTH auto_confirm
            # AND drop are supported, but a drop assignment here is justified
            # by its own NPV rule independently of whether any category ever
            # reached auto_confirm — and Task 7's activate floor re-checks
            # drop NPV from eval_observations regardless. Refusing only when
            # EVERY category stayed `review` (nothing beyond the no-op
            # default to propose) is the correct, narrower condition. Do not
            # "fix" this asymmetry to match the numeric branch.
            if all(band == "review" for band in sweep.recommended.values()):
                raise ValueError(
                    "sweep produced no recommendation on this set — every "
                    "category stayed review. Report the gap; do not propose "
                    "a no-op config."
                )
            bands_json = dict(sweep.recommended)

    # Unconditional, for both score kinds: a bare `meta.score_kind ==
    # "numeric"` gate here would let a categorical bands_json (or a numeric
    # one aimed at a categorical provider) through unparsed and unvalidated,
    # writing a row that is garbage in a shape no reader can recover from
    # cleanly — `get_config` would only discover it later, at read time.
    numeric, categorical = parse_bands(meta.score_kind, bands_json)
    if meta.score_kind == "numeric":
        problems = validate_numeric_bands(numeric, meta.score_domain)
        if problems:
            raise ValueError("; ".join(problems))
    else:
        # A mapping for a category the provider cannot emit is dead config,
        # the same way a zero-width numeric band is: it can never be reached
        # by a real response.
        known = set(meta.score_domain.categories or ())
        unknown = set(categorical) - known
        if unknown:
            raise ValueError(
                f"bands reference categories outside score_domain: "
                f"{sorted(unknown)} not in {sorted(known)}"
            )

    measured: dict[str, float | str | None]
    if bands_were_supplied:
        measured = {
            "auto_confirm_precision": None,
            "drop_npv": None,
            "note": (
                "ADVISORY ONLY — bands were supplied via --bands, not derived "
                "from this sweep; activate recomputes from eval_observations"
            ),
        }
    else:
        measured = {
            "auto_confirm_precision": _band_precision(sweep, "auto_confirm"),
            "drop_npv": _band_npv(sweep),
            "note": "ADVISORY ONLY — activate recomputes from eval_observations",
        }
    return await store.insert_config(
        provider_id=provider_id,
        version=version,
        score_kind=meta.score_kind,
        bands=bands_json,
        eval_set_id=eval_set_id,
        eval_sample_size=effective_sample_size(rows),
        measured=measured,
    )


async def run_propose(args: argparse.Namespace) -> int:
    config = load_config()
    provider_id = parse_provider_id(args.provider)
    bands_json = json.loads(args.bands) if args.bands else None
    async with make_async_pool(config.database_url, min_size=1, max_size=2) as pool:
        store = PostgresCalibrationStore(pool)
        try:
            config_id = await propose_config(
                store, provider_id, args.eval_set, args.version, bands_json
            )
        except ValueError as exc:
            print(f"refusing to propose: {exc}")
            return 1
        print(f"wrote INACTIVE config {config_id} ({args.version})")
        print(
            "  activate it with: calibrate activate --config "
            f"{config_id} --confirm --by <name>"
        )
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

    sweep = sub.add_parser(
        "sweep", help="render precision/recall/NPV across candidate boundaries"
    )
    sweep.add_argument("--provider", required=True)
    sweep.add_argument("--eval-set", required=True)
    sweep.set_defaults(func=run_sweep)

    propose = sub.add_parser(
        "propose", help="write an INACTIVE calibration config"
    )
    propose.add_argument("--provider", required=True)
    propose.add_argument("--eval-set", required=True)
    propose.add_argument("--version", required=True)
    propose.add_argument(
        "--bands", default=None,
        help="JSON bands override; omit to use the sweep's recommendation",
    )
    propose.set_defaults(func=run_propose)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func = cast(
        "Callable[[argparse.Namespace], Coroutine[Any, Any, int]]", args.func
    )
    return asyncio.run(func(args))


if __name__ == "__main__":
    sys.exit(main())
