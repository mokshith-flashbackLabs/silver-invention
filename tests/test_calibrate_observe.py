"""`calibrate observe` — fill eval_observations by calling the REAL adapter.

The adapter and the URL-matching code are the production ones. A measurement
taken against a reimplementation measures the reimplementation, so the fake
here is the provider's HTTP response, never our parsing of it.
"""

from __future__ import annotations

from decimal import Decimal

from devtools.calibrate.__main__ import observe_seed

from imageshield.search.provider import ProviderMatch, ProviderResult
from imageshield.types import ProviderId

HIVE = ProviderId("hive")


class FakeProvider:
    """Stands in for HiveWebSearchProvider at the SearchProvider boundary —
    the same Protocol the worker uses."""

    id = HIVE
    kind = "image_search"
    score_kind = "numeric"
    score_version = "hive-web-search-v1"

    def __init__(self, result: ProviderResult) -> None:
        self._result = result
        self.calls: list[str] = []

    async def search(self, seed_url: str, max_results: int | None = None) -> ProviderResult:
        self.calls.append(seed_url)
        return self._result


def result(*pages: str) -> ProviderResult:
    return ProviderResult(
        provider_id=HIVE,
        status="ok",
        matches=[
            ProviderMatch(
                image_url=f"{p}/img.jpg",
                page_urls=[p],
                provider_score=Decimal("0.93"),
                provider_category=None,
                query_quality=None,
            )
            for p in pages
        ],
        raw_response={"stub": True},
        http_status=200,
        latency_ms=10,
    )


async def test_observe_writes_an_observation_for_a_returned_candidate(
    calibration_store,
) -> None:
    await calibration_store.insert_eval_item(
        "v1", "https://seed.test/a.jpg", "https://x.test/found",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    provider = FakeProvider(result("https://x.test/found"))
    written = await observe_seed(
        calibration_store, provider, "v1", "https://seed.test/a.jpg"
    )
    assert written == 1
    rows = await calibration_store.eval_rows("v1", HIVE)
    assert rows[0].observed is True
    assert rows[0].provider_score == Decimal("0.9300")


async def test_observe_leaves_an_unreturned_candidate_unobserved(
    calibration_store,
) -> None:
    """And still records coverage — that is what turns this absence into a
    countable miss rather than an unknown."""
    await calibration_store.insert_eval_item(
        "v1", "https://seed.test/a.jpg", "https://x.test/missed",
        "true_match", "novel_generation", "synthetic, public domain", "tester",
    )
    provider = FakeProvider(result("https://x.test/something-else"))
    written = await observe_seed(
        calibration_store, provider, "v1", "https://seed.test/a.jpg"
    )
    assert written == 0
    assert await calibration_store.uncovered_seeds("v1", HIVE) == ()
    rows = await calibration_store.eval_rows("v1", HIVE)
    assert rows[0].observed is False


async def test_observe_matches_through_url_normalisation(calibration_store) -> None:
    """The labelled URL and the provider's URL differ only by tracking params
    and a trailing slash. Production dedup treats them as one page, so the
    eval matcher must too — otherwise the measurement disagrees with the
    system being measured."""
    await calibration_store.insert_eval_item(
        "v1", "https://seed.test/a.jpg", "https://X.test/Found/",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    provider = FakeProvider(result("https://x.test/Found?utm_source=twitter"))
    written = await observe_seed(
        calibration_store, provider, "v1", "https://seed.test/a.jpg"
    )
    assert written == 1


async def test_a_failed_provider_call_records_coverage_as_not_ok(
    calibration_store,
) -> None:
    await calibration_store.insert_eval_item(
        "v1", "https://seed.test/a.jpg", "https://x.test/a",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    failed = ProviderResult(
        provider_id=HIVE, status="timeout", matches=[],
        raw_response={"error": "timeout"}, http_status=None, latency_ms=120_000,
    )
    written = await observe_seed(
        calibration_store, FakeProvider(failed), "v1", "https://seed.test/a.jpg"
    )
    assert written == 0
    # NOT covered: we learned nothing about this seed, so its items' absences
    # must not later be counted as misses.
    assert await calibration_store.uncovered_seeds("v1", HIVE) == (
        "https://seed.test/a.jpg",
    )
    # uncovered_seeds alone only proves the seed isn't marked 'ok' — it would
    # read identically if record_seed_coverage were deleted outright and the
    # row never existed at all. Assert the row itself carries the real
    # failing status, not just its absence from the 'ok' set.
    async with calibration_store._pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, candidates_returned FROM eval_seed_coverage "
            "WHERE eval_set_id = %s AND seed_uri = %s AND provider_id = %s",
            ("v1", "https://seed.test/a.jpg", HIVE),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "timeout"
    assert row[1] == 0


async def test_observe_does_not_match_across_path_case(calibration_store) -> None:
    """URL normalisation lowercases the host but PRESERVES path case (v1
    rule 6 — paths are case-sensitive, hosts are not). A labelled URL and a
    returned URL that differ ONLY by path case must therefore NOT be treated
    as the same page: matching on a looser key than production's dedup key
    would measure a system that isn't the one actually running."""
    await calibration_store.insert_eval_item(
        "v1", "https://seed.test/a.jpg", "https://x.test/found",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    provider = FakeProvider(result("https://x.test/Found"))
    written = await observe_seed(
        calibration_store, provider, "v1", "https://seed.test/a.jpg"
    )
    assert written == 0
