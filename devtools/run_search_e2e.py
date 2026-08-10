"""Step-5 real-provider E2E: one seed, both providers, one run. Dev-only.

Drives the REAL PostgresSearchStore (compose Postgres) and the REAL Hive +
Google adapters with keys from .env.local, claiming and executing the run
in-process via claim_run + execute_run — the same code path the worker
takes after the SQS hop (which relay/worker tests cover with fakes).

Costs real money: one Hive Web Search call (billed per plan) + one Google
Vision call. Run deliberately, not in CI.

    .venv/Scripts/python devtools/run_search_e2e.py [seed_url]

The seed must be PUBLICLY reachable — both providers fetch the URL
themselves, so localhost/fake-s3 URLs can never work here. Default seed is
the Obama official portrait from Wikimedia Commons (public domain, widely
copied — guaranteed to have findable copies).
"""

from __future__ import annotations

import asyncio
import os
import selectors
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from imageshield.db.connection import make_async_pool
from imageshield.env import load_dotenv_local
from imageshield.providers.ratelimit import RetryPolicy
from imageshield.providers.store import PostgresProviderControlStore
from imageshield.search.cadence import CadencePolicy
from imageshield.search.google import GoogleWebDetectionProvider
from imageshield.search.hive import HiveWebSearchProvider
from imageshield.search.provider import SearchProvider
from imageshield.search.runner import execute_run
from imageshield.search.store import PostgresSearchStore
from imageshield.subjects.eligibility import eligibility_for
from imageshield.subjects.store import PostgresSubjectStore
from imageshield.types import ProviderId, UserRef

# This script builds its config from bare env vars rather than Config (it
# predates several required keys), so the step-8 policies are spelled out here
# with the same defaults config.py carries.
E2E_RETRY = RetryPolicy(max_retries=3, max_wait_seconds=30.0, jitter_fraction=0.25)
E2E_CADENCE = CadencePolicy(
    standard_days=7,
    relaxed_days=14,
    dormant_days=30,
    new_tier_weeks=4,
    relaxed_after_empty=8,
    dormant_after_empty=20,
    priority_release_after_empty=13,
)

DEFAULT_SEED_URL = (
    "https://upload.wikimedia.org/wikipedia/commons/8/8d/President_Barack_Obama.jpg"
)
DEFAULT_DB = "postgresql://imageshield:imageshield@localhost:15433/imageshield"


async def main() -> int:
    load_dotenv_local()
    seed_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SEED_URL
    database_url = os.environ.get("DATABASE_URL", DEFAULT_DB)
    hive_key = os.environ.get("HIVE_API_KEY", "")
    google_key = os.environ.get("GOOGLE_VISION_API_KEY", "")
    if not hive_key or not google_key:
        print("HIVE_API_KEY / GOOGLE_VISION_API_KEY missing from .env.local", file=sys.stderr)
        return 2

    providers: dict[ProviderId, SearchProvider] = {
        ProviderId("hive"): HiveWebSearchProvider(
            base_url=os.environ.get("HIVE_BASE_URL", "https://api.thehive.ai"),
            api_key=hive_key,
            timeout_seconds=120.0,
            retry_policy=E2E_RETRY,
        ),
        ProviderId("google"): GoogleWebDetectionProvider(
            endpoint="https://vision.googleapis.com/v1/images:annotate",
            api_key=google_key,
            timeout_seconds=60.0,
            retry_policy=E2E_RETRY,
        ),
    }

    pool = make_async_pool(database_url, min_size=1, max_size=2)
    await pool.open()
    try:
        store = PostgresSearchStore(pool)
        control = PostgresProviderControlStore(
            pool,
            cache_seconds=30.0,
            failure_threshold=5,
            default_cooldown_seconds=300,
            max_cooldown_seconds=3600,
        )
        user_ref = UserRef(uuid4())

        # Step 8: search_seeds now FKs to subjects, so the seed cannot exist
        # without one. Enrolment normally writes this row; this script has no
        # liveness session, so it asserts eligibility directly.
        await PostgresSubjectStore(pool).upsert_subject(
            user_ref, eligibility_for(True)
        )
        seed_id = await store.create_seed(user_ref, "user_supplied", seed_url)
        run_id = await store.create_run(user_ref, seed_id, tuple(providers))
        print(f"seed {seed_id}\nrun  {run_id}\nseed_url {seed_url}\n")

        claim = await store.claim_run(run_id)
        assert claim is not None, "fresh run must be claimable"
        outcome = await execute_run(
            claim, providers, store, {}, control, E2E_CADENCE
        )

        run = await store.get_run(run_id)
        assert run is not None
        print(f"\nrun status              {run.status}")
        print(f"providers_attempted     {list(run.providers_attempted)}")
        print(f"providers_succeeded     {list(run.providers_succeeded)}")
        print(f"matches_found           {run.matches_found}")
        print(f"providers_skipped       {list(outcome.providers_skipped)}")
        print(f"scan_tier               {run.scan_tier}")
        print(f"next_scan_after         {run.next_scan_after}")

        infringements = await store.list_infringements(user_ref, None)
        print(f"infringements           {len(infringements)}")
        for inf in infringements[:10]:
            print(f"  band={inf.band} providers={len(inf.attestations)} "
                  f"keyed_on={inf.keyed_on} {inf.page_url[:80]}")
            for att in inf.attestations:
                score = (
                    att.provider_score
                    if att.provider_score is not None
                    else att.provider_category
                )
                print(f"      [{att.provider_id:6}] {att.score_kind:11} {score!s:12} "
                      f"confirmed x{att.confirm_count}")
        if len(infringements) > 10:
            print(f"  ... and {len(infringements) - 10} more")

        both_ok = set(outcome.providers_succeeded) == {"hive", "google"}
        any_matches = run.matches_found > 0
        print(f"\nBOTH PROVIDERS OK: {both_ok}   MATCHES FOUND: {any_matches}")
        return 0 if both_ok and any_matches else 1
    finally:
        await pool.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        with asyncio.Runner(
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
        ) as runner:
            raise SystemExit(runner.run(main()))
    raise SystemExit(asyncio.run(main()))
