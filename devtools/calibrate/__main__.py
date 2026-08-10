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
from collections.abc import Callable, Coroutine, Mapping, Sequence
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict

from imageshield.calibration.bands import band_for_attestation, roll_up, validate_numeric_bands
from imageshield.calibration.metrics import (
    CategoricalSweep,
    EvalRow,
    Metric,
    NumericSweep,
    composition,
    confusion_at_threshold,
    confusion_for_categories,
    effective_sample_size,
    metric,
    npv,
    precision,
    sweep_categorical,
    sweep_numeric,
)
from imageshield.calibration.models import Band, CalibrationConfig, PolicyEntry
from imageshield.calibration.report import render_categorical_sweep, render_numeric_sweep
from imageshield.calibration.store import (
    AttestationDecision,
    PostgresCalibrationStore,
    ProviderMeta,
    StoredConfig,
    parse_bands,
)
from imageshield.config import Config, load_config
from imageshield.db.connection import make_async_pool
from imageshield.providers.ratelimit import policy_from_config as retry_policy_from_config
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
    retry = retry_policy_from_config(config)
    if provider_id == "hive":
        return HiveWebSearchProvider(
            base_url=config.hive_base_url,
            api_key=config.hive_api_key,
            timeout_seconds=config.provider_timeout_seconds,
            retry_policy=retry,
            client=client,
        )
    if provider_id == "google":
        return GoogleWebDetectionProvider(
            endpoint=config.google_vision_endpoint,
            api_key=config.google_vision_api_key,
            timeout_seconds=config.provider_timeout_seconds,
            retry_policy=retry,
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
    # before it. Written up front, a crash partway through the loop would
    # leave status='ok' with some observations missing — and a missing
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


# ── The floor: replay, activate, trust ──────────────────────────────────────
#
# Two gates protect the move off `review`, and they defend DIFFERENT
# failures. `check_floor` lives in code, so loosening the bar is a code
# change with a review and a git blame — it defends against deadline
# pressure. `trust` stays a separate human command and is the ONLY writer of
# `providers.calibrated` — it defends against a bad eval set producing
# good-looking numbers, which no arithmetic can catch: a sweep with no
# lookalike hard negatives yields precision 1.0 trivially, because random
# negatives are easy to reject. The floor cannot tell that set apart from a
# sound one on the numbers alone (it CAN and does reject the specific,
# unconditional case of zero lookalikes — see below — but "the lookalikes
# were not diverse enough" is a judgement, not an arithmetic fact).

# Minimum auto_confirm precision / drop NPV. Matches the target the sweep
# reports against (metrics.py's own default) rather than inventing a second
# number for the same concept.
_FLOOR_TARGET = 0.99


class FloorResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    problems: tuple[str, ...]
    auto_confirm_precision: Metric | None
    drop_npv: Metric | None
    sample_size: int
    lookalike_count: int


def _precision_of_band(
    rows: Sequence[EvalRow], config: CalibrationConfig, band: Band
) -> Metric:
    """Precision of ``band`` under this EXACT config, recomputed at the
    config's own boundary.

    Reuses ``confusion_at_threshold``/``confusion_for_categories`` — the same
    arithmetic ``sweep_numeric``/``sweep_categorical`` already use for every
    candidate boundary — rather than re-deriving predicted-positive through
    ``band_for_attestation``. That keeps this working for a boundary
    hand-typed straight into a config, not only one that happens to equal an
    observed score or a domain edge (the set ``sweep_numeric`` limits its own
    candidates to).
    """
    if config.score_kind == "numeric":
        threshold = next(
            (b.min for b in config.numeric_bands if b.band == band and b.min is not None),
            None,
        )
        if threshold is None:
            return metric(0, 0)
        return precision(confusion_at_threshold(rows, threshold))
    positive = frozenset(c for c, b in config.categorical_bands.items() if b == band)
    return precision(confusion_for_categories(rows, positive))


def _npv_of_band(
    rows: Sequence[EvalRow], config: CalibrationConfig, band: Band
) -> Metric:
    """NPV of ``band`` under this exact config — the mirror of
    :func:`_precision_of_band`, over what the config would DROP rather than
    what it would auto_confirm."""
    if config.score_kind == "numeric":
        threshold = next(
            (b.max for b in config.numeric_bands if b.band == band and b.max is not None),
            None,
        )
        if threshold is None:
            return metric(0, 0)
        return npv(confusion_at_threshold(rows, threshold))
    non_drop = frozenset(c for c, b in config.categorical_bands.items() if b != band)
    return npv(confusion_for_categories(rows, non_drop))


async def check_floor(
    store: PostgresCalibrationStore, stored: StoredConfig, min_items: int
) -> FloorResult:
    """The machine half of the two keys, recomputed FRESH from
    eval_observations joined to eval_items.

    Never reads ``calibration_configs.measured``: a check that trusts a
    JSONB column an operator can type into is defeated by editing a number.

    A review-only config skips every condition below — it alarms nobody, so
    there is nothing to gate.
    """
    config = stored.config
    declares_auto = config.declares("auto_confirm")
    declares_drop = config.declares("drop")
    if not declares_auto and not declares_drop:
        return FloorResult(
            ok=True, problems=(), auto_confirm_precision=None, drop_npv=None,
            sample_size=0, lookalike_count=0,
        )

    if stored.eval_set_id is None:
        return FloorResult(
            ok=False,
            problems=("config has no eval_set_id — nothing to measure against",),
            auto_confirm_precision=None, drop_npv=None,
            sample_size=0, lookalike_count=0,
        )

    problems: list[str] = []
    rows = await store.eval_rows(stored.eval_set_id, config.provider_id)
    comp = composition(rows)
    n = effective_sample_size(rows)

    # Unconditional and first in the narrative because it closes the real
    # failure: a set with no hard negatives yields precision 1.0 trivially,
    # and no sample size compensates for a figure that cannot be meaningful.
    if comp.lookalike_count == 0:
        problems.append(
            "eval set contains ZERO lookalike items — a set without hard "
            "negatives cannot produce a meaningful precision figure"
        )
    if n < min_items:
        problems.append(
            f"effective sample size {n} is below the floor of {min_items} "
            "(non-uncertain items only)"
        )
    uncovered = await store.uncovered_seeds(stored.eval_set_id, config.provider_id)
    if uncovered:
        problems.append(
            f"{len(uncovered)} seed(s) have no successful coverage row — their "
            "items' absences are not evidence and cannot enter recall"
        )

    ac_precision = _precision_of_band(rows, config, "auto_confirm") if declares_auto else None
    drop_npv_metric = _npv_of_band(rows, config, "drop") if declares_drop else None
    if ac_precision is not None and (
        ac_precision.value is None or ac_precision.value < _FLOOR_TARGET
    ):
        problems.append(f"auto_confirm precision {ac_precision.render()} is below 0.99")
    if drop_npv_metric is not None and (
        drop_npv_metric.value is None or drop_npv_metric.value < _FLOOR_TARGET
    ):
        problems.append(f"drop NPV {drop_npv_metric.render()} is below 0.99")

    return FloorResult(
        ok=not problems,
        problems=tuple(problems),
        auto_confirm_precision=ac_precision,
        drop_npv=drop_npv_metric,
        sample_size=n,
        lookalike_count=comp.lookalike_count,
    )


class RebandPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    decisions: tuple[AttestationDecision, ...]
    infringement_bands: Mapping[UUID, tuple[str, str]]
    attestations_changed: int
    infringements_changed: int
    users_affected: int
    by_direction: Mapping[str, int]


async def plan_reband(
    store: PostgresCalibrationStore, provider_id: ProviderId, entry: PolicyEntry
) -> RebandPlan:
    """Compute what a config WOULD do. Writes nothing — ``replay`` is exactly
    this function plus rendering, and ``activate``/``trust`` are this
    function plus ``apply_reband``.
    """
    stored = await store.attestations_for_provider(provider_id)
    existing = await store.all_attestation_bands()

    decisions: list[AttestationDecision] = []
    proposed: dict[UUID, dict[UUID, Band]] = {}
    for a in stored:
        decision = band_for_attestation(
            entry, a.score_kind, a.provider_score, a.provider_category
        )
        decisions.append(
            AttestationDecision(
                attestation_id=a.attestation_id,
                infringement_id=a.infringement_id,
                user_ref=a.user_ref,
                old_band=a.band,
                band=decision.band,
                calibration_version=decision.calibration_version,
            )
        )
        proposed.setdefault(a.infringement_id, {})[a.attestation_id] = decision.band

    infringement_bands: dict[UUID, tuple[str, str]] = {}
    for infringement_id, current in existing.items():
        # Other providers' attestations keep their existing bands; only this
        # provider's move. That is the point of a per-provider config.
        bands = [
            proposed.get(infringement_id, {}).get(attestation_id, band)
            for attestation_id, band in current
        ]
        rolled, reason = roll_up(bands)
        infringement_bands[infringement_id] = (rolled, reason)

    changed = [d for d in decisions if d.band != d.old_band]
    by_direction: dict[str, int] = {}
    for d in changed:
        key = f"{d.old_band}->{d.band}"
        by_direction[key] = by_direction.get(key, 0) + 1
    changed_infringement_ids = {d.infringement_id for d in changed}
    infringements_changed = sum(
        1 for iid in infringement_bands if iid in changed_infringement_ids
    )

    return RebandPlan(
        decisions=tuple(decisions),
        infringement_bands=infringement_bands,
        attestations_changed=len(changed),
        infringements_changed=infringements_changed,
        users_affected=len({d.user_ref for d in changed}),
        by_direction=by_direction,
    )


async def run_replay(args: argparse.Namespace) -> int:
    """Read-only. Builds the calibrated PolicyEntry this config would run
    under RIGHT NOW (the provider's real ``calibrated`` flag, not a
    hypothetical one — that hypothetical is `check_floor`'s job, not
    replay's), reports the delta, and NEVER calls ``apply_reband``."""
    config = load_config()
    async with make_async_pool(config.database_url, min_size=1, max_size=2) as pool:
        store = PostgresCalibrationStore(pool)
        stored = await store.get_config(args.config)
        if stored is None:
            print(f"no config {args.config}")
            return 1
        meta = await store.provider_meta(stored.config.provider_id)
        if meta is None:
            print(f"unknown provider {stored.config.provider_id!r}")
            return 1
        entry = PolicyEntry(
            provider_id=stored.config.provider_id,
            calibrated=meta.calibrated,
            score_domain=meta.score_domain,
            config=stored.config,
        )
        plan = await plan_reband(store, stored.config.provider_id, entry)
        print(f"config {args.config} ({stored.config.version}):")
        print(f"  attestations changed: {plan.attestations_changed}")
        print(f"  infringements changed: {plan.infringements_changed}")
        print(f"  users affected: {plan.users_affected}")
        for direction, count in sorted(plan.by_direction.items()):
            print(f"    {direction}: {count}")
    return 0


async def activate_config(
    store: PostgresCalibrationStore,
    config_id: UUID,
    activated_by: str,
    min_items: int,
) -> None:
    stored = await store.get_config(config_id)
    if stored is None:
        raise ValueError(f"no config {config_id}")
    floor = await check_floor(store, stored, min_items)
    if not floor.ok:
        raise ValueError("floor not met: " + "; ".join(floor.problems))
    meta = await store.provider_meta(stored.config.provider_id)
    assert meta is not None
    # Re-band under the provider's REAL calibrated flag, not the floor's
    # hypothetical one: activation alone must not move anything off review.
    entry = PolicyEntry(
        provider_id=stored.config.provider_id,
        calibrated=meta.calibrated,
        score_domain=meta.score_domain,
        config=stored.config,
    )
    plan = await plan_reband(store, stored.config.provider_id, entry)
    await store.apply_reband(
        stored.config.provider_id, config_id, activated_by,
        plan.decisions, plan.infringement_bands,
    )
    await store.audit(
        "calibration.activated",
        actor=activated_by,
        resource_id=config_id,
        metadata={
            "provider_id": stored.config.provider_id,
            "version": stored.config.version,
            "eval_set_id": stored.eval_set_id,
            "sample_size": floor.sample_size,
            "lookalike_count": floor.lookalike_count,
            "attestations_rebanded": len(plan.decisions),
            "users_affected": plan.users_affected,
        },
    )


async def run_activate(args: argparse.Namespace) -> int:
    if not args.confirm:
        print("refusing without --confirm (this rebands live attestations)")
        return 1
    config = load_config()
    async with make_async_pool(config.database_url, min_size=1, max_size=2) as pool:
        store = PostgresCalibrationStore(pool)
        try:
            # The floor comes from config, NEVER from the command line. It is
            # the defence against the spec's own worked example — a sweep over
            # 40 items with no hard negatives scores precision 1.0 trivially —
            # and a flag that lowers it turns "a code change with a review and
            # a git blame" into a keystroke. Tightening it is an ops change
            # (raise CALIBRATION_MIN_EVAL_ITEMS); loosening it is not
            # available here at all.
            await activate_config(
                store,
                args.config,
                activated_by=args.by,
                min_items=config.calibration_min_eval_items,
            )
        except ValueError as exc:
            print(f"refusing to activate: {exc}")
            return 1
        print(f"activated config {args.config}")
    return 0


async def trust_provider(
    store: PostgresCalibrationStore,
    provider_id: ProviderId,
    *,
    trusted: bool,
    actor: str,
    reason: str,
) -> None:
    """The second key, and the only writer of ``providers.calibrated``.

    "This config is sound" and "this provider may now alarm people without a
    human looking" are different claims. The first is arithmetic and the
    floor checks it. The second is judgement about whether the eval set
    resembles the real world, and no code can check that.
    """
    if not reason.strip():
        raise ValueError("--reason is required and must not be blank")
    await store.set_calibrated(provider_id, trusted, actor, reason)
    # Re-band under the new flag: revoking must actually put everything back
    # to review, or the flag is decorative.
    meta = await store.provider_meta(provider_id)
    assert meta is not None
    active = await store.active_config(provider_id)
    entry = PolicyEntry(
        provider_id=provider_id,
        calibrated=meta.calibrated,
        score_domain=meta.score_domain,
        config=active,
    )
    plan = await plan_reband(store, provider_id, entry)
    await store.apply_reband(
        provider_id, None, None, plan.decisions, plan.infringement_bands
    )


async def run_trust(args: argparse.Namespace) -> int:
    if not args.confirm:
        print("refusing without --confirm (this can alarm or silence real users)")
        return 1
    config = load_config()
    provider_id = parse_provider_id(args.provider)
    async with make_async_pool(config.database_url, min_size=1, max_size=2) as pool:
        store = PostgresCalibrationStore(pool)
        try:
            await trust_provider(
                store, provider_id,
                trusted=not args.revoke, actor=args.by, reason=args.reason,
            )
        except ValueError as exc:
            print(f"refusing to trust: {exc}")
            return 1
        print(f"{'revoked' if args.revoke else 'trusted'} provider {provider_id}")
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

    replay = sub.add_parser(
        "replay", help="show what activating this config would change; writes nothing"
    )
    replay.add_argument("--config", required=True, type=UUID)
    replay.set_defaults(func=run_replay)

    activate = sub.add_parser(
        "activate", help="activate a config after it clears the floor"
    )
    activate.add_argument("--config", required=True, type=UUID)
    activate.add_argument("--by", required=True)
    activate.add_argument(
        "--confirm", action="store_true", help="required: this rebands live attestations"
    )
    activate.set_defaults(func=run_activate)

    trust = sub.add_parser(
        "trust",
        help="the second key: allow (or --revoke) a provider auto_confirm/drop",
    )
    trust.add_argument("--provider", required=True)
    trust.add_argument("--by", required=True)
    trust.add_argument("--reason", required=True)
    trust.add_argument(
        "--revoke", action="store_true", help="revoke trust instead of granting it"
    )
    trust.add_argument(
        "--confirm", action="store_true",
        help="required: this can alarm or silence real users",
    )
    trust.set_defaults(func=run_trust)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func = cast(
        "Callable[[argparse.Namespace], Coroutine[Any, Any, int]]", args.func
    )
    return asyncio.run(func(args))


if __name__ == "__main__":
    sys.exit(main())
