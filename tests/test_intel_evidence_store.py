"""The evidence store against real Postgres (tests/db.py harness)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.db.connection import make_async_pool
from imageshield.intel.evidence_store import (
    DocumentRecord,
    PostgresEvidenceStore,
    SignalRecord,
    SnapshotRecord,
)
from imageshield.intel.store import PostgresIntelStore
from imageshield.intel.verify import VerifiedQuote
from tests.db import run_migrate


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    assert run_migrate(throwaway_db, "down", "--all").returncode == 0
    assert run_migrate(throwaway_db, "up").returncode == 0
    return throwaway_db


@pytest.fixture
async def stores(
    migrated_db: str,
) -> AsyncIterator[tuple[PostgresIntelStore, PostgresEvidenceStore]]:
    pool: AsyncConnectionPool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresIntelStore(pool), PostgresEvidenceStore(pool)
    finally:
        await pool.close()


def _doc(run_id: UUID, source_id: UUID | None = None, url_hash: str = "c" * 64) -> DocumentRecord:
    return DocumentRecord(
        run_id=run_id,
        source_id=source_id,
        document_url="https://p.example/t",
        final_url="https://p.example/t",
        url_hash=url_hash,
        publisher_domain="example.com",
        trust="listed",
        content_sha256="d" * 64,
        truncated=False,
        title="Terms",
        published_at=None,
    )


def _signal() -> SignalRecord:
    quote = VerifiedQuote("public profile photos may be used for AI training", 10, 60, "e" * 64)
    return SignalRecord(
        category="policy",
        direction="risk_up",
        tags=("instagram",),
        unregistered_subjects=(),
        summary="Default AI training on public photos.",
        model_id="claude-sonnet-5",
        prompt_version="extract-v1",
        quotes=(quote,),
    )


async def test_record_unit_writes_document_signal_excerpt_snapshot_and_hash(stores) -> None:
    intel, evidence = stores
    source = await intel.create_source(
        kind="policy_page",
        source_url="https://p.example/t",
        query_text=None,
        tags=(),
        check_every_hours=24,
        terms_note="automated access permitted",
        operator="a",
    )
    run_id = await intel.queue_source_check(source.source_id, operator="a")
    assert run_id is not None
    snap = SnapshotRecord(
        source_id=source.source_id,
        content_sha256="d" * 64,
        snapshot_text="masked text",
        content_type="text/html",
        truncated=False,
    )
    document_id = await evidence.record_unit(
        _doc(run_id, source.source_id),
        [_signal()],
        snapshot=snap,
        source_hash=(source.source_id, "d" * 64),
    )
    assert document_id is not None
    assert (await evidence.snapshot_for(source.source_id)) == snap
    refreshed = await intel.get_source(source.source_id)
    assert refreshed is not None and refreshed.last_content_sha256 == "d" * 64
    (signal,) = await evidence.list_signals(cursor=None, limit=10)
    assert signal["document_id"] == document_id
    assert signal["tags"] == ["instagram"]
    detail = await evidence.get_signal(signal["signal_id"])
    assert detail is not None and len(detail["excerpts"]) == 1
    assert detail["document"]["document_id"] == document_id
    assert detail["document"]["document_url_hash"] == detail["document"]["url_hash"]


async def test_a_reclaimed_run_does_not_duplicate_a_document(stores) -> None:
    intel, evidence = stores
    run_id = await intel.queue_adhoc("https://p.example/t", operator="a")
    assert await evidence.record_unit(_doc(run_id), [_signal()], snapshot=None, source_hash=None)
    assert (
        await evidence.record_unit(_doc(run_id), [_signal()], snapshot=None, source_hash=None)
        is None
    )
    assert len(await evidence.list_signals(cursor=None, limit=10)) == 1


async def test_a_requested_url_that_redirects_writes_both_hashes(stores) -> None:
    intel, evidence = stores
    run_id = await intel.queue_adhoc("https://p.example/redirected", operator="a")
    document = DocumentRecord(
        run_id=run_id,
        document_url="https://p.example/redirected",
        final_url="https://p.example/final",
        url_hash="f" * 64,
        document_url_hash="r" * 64,
        publisher_domain="example.com",
        trust="listed",
        content_sha256="d" * 64,
        truncated=False,
        title="Terms",
        published_at=None,
    )
    assert await evidence.record_unit(document, [], snapshot=None, source_hash=None) is not None
    # Recognisable by EITHER hash — the redirect's target or the URL originally requested.
    assert await evidence.recently_fetched(["f" * 64], days=1) == {"f" * 64}
    assert await evidence.recently_fetched(["r" * 64], days=1) == {"r" * 64}
    assert await evidence.recently_fetched(["z" * 64], days=1) == set()
    assert await evidence.recently_fetched(["f" * 64], days=0) == set()


async def test_seen_url_hashes_is_scoped_to_its_source(stores) -> None:
    intel, evidence = stores
    source_a = await intel.create_source(
        kind="policy_page",
        source_url="https://a.example/terms",
        query_text=None,
        tags=(),
        check_every_hours=24,
        terms_note="automated access permitted",
        operator="a",
    )
    source_b = await intel.create_source(
        kind="policy_page",
        source_url="https://b.example/terms",
        query_text=None,
        tags=(),
        check_every_hours=24,
        terms_note="automated access permitted",
        operator="a",
    )
    run_id = await intel.queue_source_check(source_a.source_id, operator="a")
    assert run_id is not None
    await evidence.record_unit(
        _doc(run_id, source_a.source_id, url_hash="a" * 64), [], snapshot=None, source_hash=None
    )
    assert await evidence.seen_url_hashes(source_a.source_id, ["a" * 64, "b" * 64]) == {"a" * 64}
    assert await evidence.seen_url_hashes(source_b.source_id, ["a" * 64]) == set()


async def test_consecutive_failures_disable_the_source(stores) -> None:
    intel, evidence = stores
    source = await intel.create_source(
        kind="news",
        source_url="https://n.example/",
        query_text=None,
        tags=(),
        check_every_hours=24,
        terms_note="automated access permitted",
        operator="a",
    )
    for _ in range(9):
        await evidence.record_check(source.source_id, ok=False, status="failed")
    still_enabled = await intel.get_source(source.source_id)
    assert still_enabled is not None and still_enabled.enabled
    await evidence.record_check(source.source_id, ok=False, status="failed")
    refreshed = await intel.get_source(source.source_id)
    assert (
        refreshed is not None
        and not refreshed.enabled
        and refreshed.disabled_reason == "unreachable"
    )
    # A success resets the count and never re-enables it on its own.
    await evidence.record_check(source.source_id, ok=True, status="ok")
    reset = await intel.get_source(source.source_id)
    assert reset is not None and reset.consecutive_failures == 0 and not reset.enabled


async def test_disable_source_writes_the_reason_directly(stores) -> None:
    intel, evidence = stores
    source = await intel.create_source(
        kind="feed",
        source_url="https://f.example/rss",
        query_text=None,
        tags=(),
        check_every_hours=24,
        terms_note="automated access permitted",
        operator="a",
    )
    await evidence.disable_source(source.source_id, reason="too_short")
    refreshed = await intel.get_source(source.source_id)
    assert (
        refreshed is not None and not refreshed.enabled and refreshed.disabled_reason == "too_short"
    )


async def test_retract_is_terminal(stores) -> None:
    intel, evidence = stores
    run_id = await intel.queue_adhoc("https://p.example/t", operator="a")
    await evidence.record_unit(_doc(run_id), [_signal()], snapshot=None, source_hash=None)
    (signal,) = await evidence.list_signals(cursor=None, limit=10)
    assert (
        await evidence.retract_signal(signal["signal_id"], operator="a", reason="wrong")
        == "retracted"
    )
    assert (
        await evidence.retract_signal(signal["signal_id"], operator="a", reason="again")
        == "not_active"
    )
    assert await evidence.retract_signal(uuid4(), operator="a", reason="x") == "not_found"
