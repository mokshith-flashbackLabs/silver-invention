"""PostgresPreviewStore against real Postgres (fixture convention of
tests/test_confirm_store.py: own down --all + up arrange step, direct SQL for
fixtures).

The load-bearing behaviours: the ownership/visibility ``None`` is one
indistinguishable answer for absent / not-yours / quarantined / duplicate
(the feedback endpoint's 404-oracle discipline), and the render audit trail
both records and rate-limits (INVARIANTS #31/#32)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from imageshield.confirm.models import AUTO_CONFIRM_DECIDED_BY
from imageshield.db.connection import make_async_pool
from imageshield.preview.store import PostgresPreviewStore
from imageshield.types import UserRef
from tests.db import run_migrate

BBOX = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


@pytest.fixture
async def store(migrated_db: str) -> AsyncIterator[PostgresPreviewStore]:
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresPreviewStore(pool)
    finally:
        await pool.close()


def _user() -> UserRef:
    return UserRef(uuid4())


def _infringement(
    conn: psycopg.Connection[Any],
    user_ref: UserRef,
    *,
    confirm_state: str = "machine_triaged",
    image_url: str | None = "set",
    preview_image_url: str | None = None,
    severity: str | None = None,
    confirm_decided_by: str | None = None,
) -> UUID:
    url_hash = uuid4().hex + uuid4().hex
    url = f"https://example.test/{uuid4().hex}"
    row = conn.execute(
        "INSERT INTO content_urls (url_hash, url, source_domain, canonical_url)"
        " VALUES (%s, %s, 'example.test', %s)",
        (url_hash, url, url),
    )
    # 0021's infringements_confirmed_needs_human CHECK wants BOTH decided
    # columns whenever confirm_state is 'confirmed'.
    decided_at = "now()" if confirm_decided_by else "NULL"
    row = conn.execute(
        "INSERT INTO infringements"
        " (user_ref, url_hash, page_url, image_url, confirm_state, preview_image_url,"
        " severity, confirm_decided_by, confirm_decided_at)"
        f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, {decided_at})"
        " RETURNING infringement_id",
        (
            user_ref,
            url_hash,
            url,
            f"{url}.jpg" if image_url == "set" else image_url,
            confirm_state,
            preview_image_url,
            severity,
            confirm_decided_by,
        ),
    ).fetchone()
    assert row is not None
    infringement_id: UUID = row[0]
    return infringement_id


def _task(
    conn: psycopg.Connection[Any],
    infringement_id: UUID,
    user_ref: UserRef,
    *,
    triage: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO review_tasks (infringement_id, user_ref, severity, triage)"
        " VALUES (%s, %s, 'benign_copy', %s)",
        (infringement_id, user_ref, Jsonb(triage)),
    )


# ── target ────────────────────────────────────────────────────────────────


async def test_owner_gets_image_url_and_bbox(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref)
        _task(conn, infringement_id, user_ref, triage={"best_face_bbox": BBOX})

    target = await store.target(infringement_id, user_ref)

    assert target is not None
    assert target.image_url is not None and target.image_url.endswith(".jpg")
    assert target.bbox == BBOX


async def test_prefers_the_resolved_page_preview_over_the_provider_url(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    """0030. When the provider keyed the hit on a page, `image_url` is not
    fetchable (Google's page entries carry no image address) and the preview
    must come from the og:image we resolved. Reading `image_url` here is what
    left 11 of 12 real hits with no picture on 2026-09-07."""
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(
            conn, user_ref, preview_image_url="https://cdn.test/resolved.jpg"
        )
        _task(conn, infringement_id, user_ref, triage={"best_face_bbox": BBOX})

    target = await store.target(infringement_id, user_ref)

    assert target is not None
    assert target.image_url == "https://cdn.test/resolved.jpg"


async def test_falls_back_to_the_provider_url_when_nothing_was_resolved(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    """The common case is unchanged: a provider that gave us a real image URL
    needs no page resolution, and `preview_image_url` stays NULL."""
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref, preview_image_url=None)
        _task(conn, infringement_id, user_ref, triage={"best_face_bbox": BBOX})

    target = await store.target(infringement_id, user_ref)

    assert target is not None
    assert target.image_url is not None and target.image_url.endswith(".jpg")


async def test_none_when_neither_url_is_present(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(
            conn, user_ref, image_url=None, preview_image_url=None
        )
        _task(conn, infringement_id, user_ref, triage={"best_face_bbox": BBOX})

    target = await store.target(infringement_id, user_ref)

    assert target is not None
    assert target.image_url is None


async def test_wrong_user_ref_is_none(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref)
        _task(conn, infringement_id, user_ref, triage={"best_face_bbox": BBOX})

    assert await store.target(infringement_id, _user()) is None


@pytest.mark.parametrize("state", ["quarantined", "duplicate"])
async def test_invisible_states_are_none(
    migrated_db: str, store: PostgresPreviewStore, state: str
) -> None:
    """To the subject these rows do not exist — same answer as absent, so the
    response can never confirm that a quarantined hit is out there."""
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        if state == "duplicate":
            original = _infringement(conn, user_ref)
            infringement_id = _infringement(conn, user_ref, confirm_state="unconfirmed")
            conn.execute(
                "UPDATE infringements SET confirm_state = 'duplicate', duplicate_of = %s"
                " WHERE infringement_id = %s",
                (original, infringement_id),
            )
        else:
            infringement_id = _infringement(conn, user_ref, confirm_state=state)

    assert await store.target(infringement_id, user_ref) is None


async def test_untriaged_hit_has_no_bbox_but_is_not_none(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    """No review_tasks row yet = 'being checked': the route answers
    preview_unavailable, not the ownership 404."""
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref, confirm_state="unconfirmed")

    target = await store.target(infringement_id, user_ref)

    assert target is not None
    assert target.bbox is None


async def test_malformed_bbox_degrades_to_none(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    """A malformed triage bbox must become 'no preview', never a fetcher call
    with garbage coordinates."""
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref)
        _task(
            conn,
            infringement_id,
            user_ref,
            triage={"best_face_bbox": {"x": 0.1, "y": "not a number"}},
        )

    target = await store.target(infringement_id, user_ref)

    assert target is not None
    assert target.bbox is None


@pytest.mark.parametrize("decided_by", [AUTO_CONFIRM_DECIDED_BY, "ops@imageshield"])
async def test_a_confirmed_ncii_hit_is_flagged_restricted(
    migrated_db: str, store: PostgresPreviewStore, decided_by: str
) -> None:
    """2026-09-14. The flag is keyed on confirm_state + severity, NOT on the
    `auto:nsfw` marker, so an OPERATOR-confirmed `ncii_suspected` hit is
    restricted too -- which is how the backend presents the same two columns.
    One predicate on both sides is what stops them drifting apart.

    `target()` still answers, and still reports a renderable picture: the
    refusal is the route's. `svc.v_person_hits.preview_available` mirrors this
    query's renderability logic row by row (0031) and that column means "a
    picture exists" -- collapsing this to None would silently redefine it.
    """
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(
            conn,
            user_ref,
            confirm_state="confirmed",
            severity="ncii_suspected",
            confirm_decided_by=decided_by,
        )
        _task(conn, infringement_id, user_ref, triage={"best_face_bbox": BBOX})

    target = await store.target(infringement_id, user_ref)

    assert target is not None
    assert target.restricted is True
    assert target.image_url is not None
    assert target.bbox == BBOX


@pytest.mark.parametrize(
    "confirm_state,severity,decided_by",
    [
        ("machine_triaged", "ncii_suspected", None),
        ("confirmed", "explicit_unmatched", "ops@imageshield"),
        ("machine_triaged", "likely_not_subject", None),
    ],
)
async def test_every_other_hit_is_not_restricted(
    migrated_db: str,
    store: PostgresPreviewStore,
    confirm_state: str,
    severity: str,
    decided_by: str | None,
) -> None:
    """Both halves of the predicate are load-bearing. A machine-triaged
    `ncii_suspected` hit is still the ordinary ask-card -- nothing has decided
    it -- and a confirmed `explicit_unmatched` one is a hit the subject
    answered themselves, which they may still look at."""
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(
            conn,
            user_ref,
            confirm_state=confirm_state,
            severity=severity,
            confirm_decided_by=decided_by,
        )
        _task(conn, infringement_id, user_ref, triage={"best_face_bbox": BBOX})

    target = await store.target(infringement_id, user_ref)

    assert target is not None
    assert target.restricted is False


# ── render audit + ceiling ────────────────────────────────────────────────


async def test_record_render_writes_the_audit_row_and_counts(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref)

    await store.record_render(user_ref, infringement_id, reveal=True)

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        row = conn.execute(
            "SELECT actor_type, action, subject_ref, resource_id, metadata"
            " FROM audit_log WHERE action = 'preview.rendered'",
        ).fetchone()
    assert row is not None
    assert row[0] == "subject"
    assert row[2] == user_ref
    assert row[3] == infringement_id
    assert row[4] == {"reveal": True}
    assert await store.renders_last_24h(user_ref) == 1


async def test_ceiling_count_scopes_to_user_and_window(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    user_ref = _user()
    other = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref)
        # Another user's render and a stale render (25h old) must not count.
        conn.execute(
            "INSERT INTO audit_log (actor_type, action, subject_ref, resource_id, metadata)"
            " VALUES ('subject', 'preview.rendered', %s, %s, '{}'::jsonb)",
            (other, infringement_id),
        )
        conn.execute(
            "INSERT INTO audit_log"
            " (actor_type, action, subject_ref, resource_id, metadata, occurred_at)"
            " VALUES ('subject', 'preview.rendered', %s, %s, '{}'::jsonb,"
            " now() - interval '25 hours')",
            (user_ref, infringement_id),
        )

    await store.record_render(user_ref, infringement_id, reveal=False)

    assert await store.renders_last_24h(user_ref) == 1


# ── the operator half (2026-09-14) ────────────────────────────────────────


async def test_operator_target_needs_no_ownership_but_refuses_a_quarantine(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    """An operator is authorised by the admin token, not by owning the hit --
    so there is no `user_ref` predicate, and the target comes back for a hit
    belonging to somebody the reviewer has never heard of. What is still
    refused is a quarantined hit: CSAM-suspected, rendered to nobody, ever.
    """
    owner = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        ordinary = _infringement(conn, owner)
        _task(conn, ordinary, owner, triage={"best_face_bbox": BBOX})
        quarantined = _infringement(conn, owner, confirm_state="quarantined")
        _task(conn, quarantined, owner, triage={"best_face_bbox": BBOX})

    target = await store.operator_target(ordinary)
    refused = await store.operator_target(quarantined)

    assert target is not None
    assert target.image_url is not None and target.image_url.endswith(".jpg")
    assert target.bbox == BBOX
    # The audit row names whose hit was rendered, so the query has to say.
    assert target.user_ref == owner
    assert refused is None
    assert await store.operator_target(uuid4()) is None


async def test_operator_target_still_offers_a_restricted_finding(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    """The SUBJECT is refused a confirmed `ncii_suspected` hit (2026-09-14,
    D4) because they are shown nothing and asked nothing about it. A reviewer
    is not: auto-confirm is the one place in this pipeline where no human ever
    looked, so it is the one a false-positive review most needs to check."""
    owner = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(
            conn,
            owner,
            confirm_state="confirmed",
            severity="ncii_suspected",
            confirm_decided_by=AUTO_CONFIRM_DECIDED_BY,
        )
        _task(conn, infringement_id, owner, triage={"best_face_bbox": BBOX})

    subject_view = await store.target(infringement_id, owner)
    operator_view = await store.operator_target(infringement_id)

    assert subject_view is not None and subject_view.restricted is True
    assert operator_view is not None
    assert operator_view.bbox == BBOX


async def test_an_operator_render_does_not_spend_the_subjects_ceiling(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    """The two ceilings are counted separately, and this is why: without the
    `actor_type = 'subject'` predicate on the subject count, a reviewer
    working through somebody's hits would exhaust that person's own daily
    allowance and lock them out of their report."""
    owner = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, owner)

    await store.record_operator_render(
        "alice", owner, infringement_id, reveal=False
    )
    await store.record_operator_render("alice", owner, infringement_id, reveal=True)

    assert await store.renders_last_24h(owner) == 0
    assert await store.operator_renders_last_24h("alice") == 2


async def test_the_operator_ceiling_counts_per_operator_and_window(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    """Per operator, not per console: a compromised account is the abuse case,
    and one shared counter would let it hide behind everybody else's quiet
    day. A stale render (25h old) has aged out of the rolling window."""
    owner = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, owner)
        conn.execute(
            "INSERT INTO audit_log"
            " (actor_type, action, subject_ref, resource_id, metadata, occurred_at)"
            " VALUES ('operator', 'preview.rendered', %s, %s,"
            " '{\"operator\": \"alice\"}'::jsonb, now() - interval '25 hours')",
            (owner, infringement_id),
        )

    await store.record_operator_render("alice", owner, infringement_id, reveal=False)
    await store.record_operator_render("bob", owner, infringement_id, reveal=False)

    assert await store.operator_renders_last_24h("alice") == 1
    assert await store.operator_renders_last_24h("bob") == 1
    assert await store.operator_renders_last_24h("carol") == 0


async def test_both_viewers_write_one_action_distinguished_by_actor_type(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    """ONE action name for both, so "every render of this hit, by anybody" is
    a single filter -- which is what an audit of a specific person's imagery
    has to be able to ask. The operator's name rides in the metadata because
    `subject_ref` is already spoken for: it names whose hit was rendered, for
    both viewers."""
    owner = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, owner)

    await store.record_render(owner, infringement_id, reveal=False)
    await store.record_operator_render("alice", owner, infringement_id, reveal=True)

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT actor_type, subject_ref, metadata FROM audit_log"
            " WHERE action = 'preview.rendered' AND resource_id = %s"
            " ORDER BY actor_type",
            (infringement_id,),
        ).fetchall()

    assert [row[0] for row in rows] == ["operator", "subject"]
    # subject_ref is the hit's OWNER in both rows, so the by-person filter
    # works for both viewers.
    assert [row[1] for row in rows] == [owner, owner]
    assert rows[0][2] == {"reveal": True, "operator": "alice"}
    assert rows[1][2] == {"reveal": False}
