"""The protection score store — the ONE writer of ``protection_scores`` and
``score_events`` (INVARIANTS #21 extended; enforced by ``tests/test_boundaries
.py::test_only_the_score_store_writes_the_score``, not by discipline alone).

``recompute`` is the whole module's reason to exist: one Postgres transaction
that reads everything the arithmetic in :mod:`imageshield.score.engine` needs,
syncs the recommendation set against the catalog in
:mod:`imageshield.recommendations.catalog`, diffs the freshly computed
:class:`~imageshield.score.engine.Components` against whatever is currently
stored, and journals only what moved. The seven steps below are numbered
because the order is load-bearing (design doc §6 / Task 12 brief) — this is
not a list of things that happen to run in this sequence, it is the
transaction's actual shape:

1. Does the subject exist at all? No row, no score — never invent one for an
   unknown ``user_ref``.
2. Lock the current ``protection_scores`` row (``FOR UPDATE``) so a concurrent
   recompute for the same person cannot interleave with this one's
   read-diff-write. Absent row means baseline zero — this person has never
   been scored.
3. Load every input the arithmetic needs, all inside this same transaction so
   the view is consistent: enrolment, seeds, monitored sources, confirmed
   hits (with the exposure suspension rule), threats, and which matched
   threat events still need a priority scan.
4. Sync the recommendation set (expire past ``expires_at``, complete what the
   catalog no longer desires, insert what it newly desires and nothing
   dismissed blocks) — THEN re-read the aged-open-recommendation count, since
   the posture component has to see the set as it stands *after* the sync,
   not before it.
5. Compute the new components and diff them against the locked baseline, one
   component at a time in a fixed order (posture, coverage, exposure,
   threat) so ``score_events`` reads as a deterministic story rather than an
   unordered bag of deltas. Every nonzero delta gets one journal row with a
   running ``score_after``.
6. Upsert the materialized ``protection_scores`` row.
7. If nothing moved, none of steps 5-6 touched the database — recompute is
   idempotent by construction, and a caller re-running it (the tick, a
   duplicate event) leaves no trace.

Every other write path in this codebase must go through here. That is what
the boundary test enforces, and it is why a component can never drift from
its journal: the two are written in the same statement group, in the same
transaction, by the same function.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict

from imageshield.recommendations.catalog import EventNeedingScan, RecSpec, desired
from imageshield.score.engine import (
    Components,
    ConfirmedHit,
    ScoreState,
    ScoreWeights,
    ThreatPenalty,
    compute,
)
from imageshield.types import UserRef, parse_user_ref

# ── step 1 ────────────────────────────────────────────────────────────────
_GET_SUBJECT_SQL = "SELECT 1 FROM subjects WHERE user_ref = %(user_ref)s"

# ── step 2 ────────────────────────────────────────────────────────────────
# FOR UPDATE: two concurrent recomputes for the same person (a tick sweep
# overlapping a hit-driven recompute) must not both read the same baseline and
# both write a diff against it — the same reasoning as search/store.py's
# _LOCK_SEED_FOR_CADENCE_SQL.
_LOCK_SCORE_SQL = """
    SELECT score, components FROM protection_scores
    WHERE user_ref = %(user_ref)s
    FOR UPDATE
"""

# ── step 3: state load ───────────────────────────────────────────────────
_ENROLMENT_ACTIVE_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM enrolments WHERE user_ref = %(user_ref)s AND status = 'active'
    )
"""

_SEED_STATE_SQL = """
    SELECT
      count(*)::int AS seed_count,
      COALESCE(
        bool_or(created_at > now() - make_interval(days => %(fresh_days)s)), false
      ) AS seeds_fresh,
      COALESCE(
        bool_or(
          next_scan_after IS NOT NULL
          AND next_scan_after < now() - make_interval(days => %(grace_days)s)
        ), false
      ) AS has_overdue_scan
    FROM search_seeds
    WHERE user_ref = %(user_ref)s AND status = 'active'
"""

# The 0016 v_person_report_summary `m` join, unchanged: providers that
# ACTUALLY RETURNED for this person and are still enabled — not configured
# ones (CLAUDE.md §7.5).
_MONITORED_SOURCES_SQL = """
    SELECT count(DISTINCT p.provider_id)::int
    FROM search_runs sr
    CROSS JOIN LATERAL unnest(sr.providers_succeeded) AS p(provider_id)
    JOIN providers pr ON pr.provider_id = p.provider_id AND pr.enabled
    WHERE sr.status = 'completed' AND sr.user_ref = %(user_ref)s
"""

# Only HUMAN-confirmed hits feed exposure (INVARIANTS #19/#47 — never machine
# triage). `counts` folds three independent reasons a confirmed hit stops
# costing exposure points into one boolean the engine can just read:
#   - the URL died (recheck loop sets url_alive false — not built in v1, but
#     the column exists and a test writes it directly);
#   - the user's own terminal position (`dismissed_not_me` / `authorised`,
#     the 0016 vocabulary);
#   - a PENDING `not_me` review — the latest feedback row says `not_me` but
#     `infringements.status` has not (yet, or ever, via a path other than
#     record_feedback) caught up. Both checks stay in, deliberately
#     redundant: record_feedback's real path sets them together, but nothing
#     here should depend on that being the only way a row gets written.
# The 0016 DISTINCT ON idiom for "what did they last say", plus a second CTE
# for "did they ever say anything" — awaiting_feedback is its own signal, not
# derivable from the latest row alone (a hit with feedback rows all superseded
# is not the same as a hit with none).
_CONFIRMED_HITS_SQL = """
    WITH latest_feedback AS (
      SELECT DISTINCT ON (infringement_id) infringement_id, signal
      FROM infringement_feedback
      ORDER BY infringement_id, created_at DESC, feedback_id
    ),
    feedback_counts AS (
      SELECT infringement_id, count(*)::int AS n
      FROM infringement_feedback
      GROUP BY infringement_id
    )
    SELECT
      i.severity,
      (
        i.url_alive
        AND i.status NOT IN ('dismissed_not_me', 'authorised')
        AND (lf.signal IS DISTINCT FROM 'not_me')
      ) AS counts,
      COALESCE(fc.n, 0) = 0 AS no_feedback
    FROM infringements i
    LEFT JOIN latest_feedback lf ON lf.infringement_id = i.infringement_id
    LEFT JOIN feedback_counts fc ON fc.infringement_id = i.infringement_id
    WHERE i.user_ref = %(user_ref)s AND i.confirm_state = 'confirmed'
"""

_THREATS_SQL = """
    SELECT e.penalty, e.decay_days, e.is_global,
           GREATEST(0, EXTRACT(epoch FROM now() - e.starts_at) / 86400)::int AS age_days
    FROM threat_event_matches m
    JOIN threat_events e USING (event_id)
    WHERE m.user_ref = %(user_ref)s AND e.status = 'active' AND e.expires_at > now()
"""

# "Needs a priority scan" = matched, active, unexpired, and no completed run
# has looked *since the event started* — a run that finished before the event
# began tells us nothing about it.
_EVENTS_NEEDING_SCAN_SQL = """
    SELECT m.event_id, e.expires_at
    FROM threat_event_matches m
    JOIN threat_events e USING (event_id)
    WHERE m.user_ref = %(user_ref)s AND e.status = 'active' AND e.expires_at > now()
      AND NOT EXISTS (
        SELECT 1 FROM search_runs sr
        WHERE sr.user_ref = m.user_ref
          AND sr.status = 'completed'
          AND sr.completed_at > e.starts_at
      )
"""

# ── step 4: recommendation sync ──────────────────────────────────────────
_EXPIRE_RECS_SQL = """
    UPDATE recommendations
    SET status = 'expired'
    WHERE user_ref = %(user_ref)s AND status = 'open'
      AND expires_at IS NOT NULL AND expires_at <= %(now)s
"""

_OPEN_RECS_SQL = """
    SELECT rec_id, kind, source_event_id
    FROM recommendations
    WHERE user_ref = %(user_ref)s AND status = 'open'
"""

_COMPLETE_REC_SQL = """
    UPDATE recommendations SET status = 'completed', completed_at = now()
    WHERE rec_id = %(rec_id)s
"""

# The 0022 partial unique index's own COALESCE sentinel, mirrored exactly so
# the ON CONFLICT target matches it: NULL source_event_id collapses to the
# sentinel UUID for the uniqueness check. Insert is skipped, atomically,
# either because an open row with this (kind, source_event_id) already exists
# (ON CONFLICT DO NOTHING) or because the person dismissed this exact
# recommendation before (the NOT EXISTS guard) — a dismissed row is not
# 'open' so the index alone would not have stopped a re-insert.
_INSERT_REC_SQL = """
    INSERT INTO recommendations (user_ref, kind, params, source_event_id, expires_at)
    SELECT %(user_ref)s, %(kind)s, %(params)s, %(source_event_id)s, %(expires_at)s
    WHERE NOT EXISTS (
      SELECT 1 FROM recommendations
      WHERE user_ref = %(user_ref)s AND kind = %(kind)s
        AND COALESCE(source_event_id, '00000000-0000-0000-0000-000000000000'::uuid)
          = COALESCE(%(source_event_id)s::uuid, '00000000-0000-0000-0000-000000000000'::uuid)
        AND status = 'dismissed'
    )
    ON CONFLICT (user_ref, kind,
                 COALESCE(source_event_id, '00000000-0000-0000-0000-000000000000'::uuid))
    WHERE status = 'open'
    DO NOTHING
"""

_AGED_OPEN_RECS_SQL = """
    SELECT count(*)::int
    FROM recommendations
    WHERE user_ref = %(user_ref)s AND status = 'open'
      AND created_at < now() - make_interval(days => %(soft_age_days)s)
"""

# ── step 5 / 6 ────────────────────────────────────────────────────────────
_INSERT_SCORE_EVENT_SQL = """
    INSERT INTO score_events (user_ref, delta, component, cause_kind, cause_ref,
                              config_version, score_after)
    VALUES (%(user_ref)s, %(delta)s, %(component)s, %(cause_kind)s, %(cause_ref)s,
            %(config_version)s, %(score_after)s)
"""

_UPSERT_SCORE_SQL = """
    INSERT INTO protection_scores (user_ref, score, components, config_version, computed_at)
    VALUES (%(user_ref)s, %(score)s, %(components)s, %(config_version)s, now())
    ON CONFLICT (user_ref) DO UPDATE
      SET score = EXCLUDED.score,
          components = EXCLUDED.components,
          config_version = EXCLUDED.config_version,
          computed_at = now()
"""

# ── read paths ────────────────────────────────────────────────────────────
_GET_SCORE_SQL = """
    SELECT score, components, config_version, computed_at
    FROM protection_scores WHERE user_ref = %(user_ref)s
"""

_LIST_EVENTS_SQL = """
    SELECT score_event_id, delta, component, cause_kind, cause_ref, config_version,
           score_after, created_at
    FROM score_events
    WHERE user_ref = %(user_ref)s
    ORDER BY score_event_id DESC
    LIMIT %(limit)s
"""

_ALL_SUBJECTS_SQL = "SELECT user_ref FROM subjects ORDER BY user_ref"

_EXPIRE_DUE_THREAT_EVENTS_SQL = """
    UPDATE threat_events SET status = 'expired', updated_at = now()
    WHERE status = 'active' AND expires_at <= %(now)s
"""

_COMPONENT_ORDER: tuple[str, ...] = ("posture", "coverage", "exposure", "threat")


class ScoreResult(BaseModel):
    """What one ``recompute`` call produced. ``changed=False`` means the
    transaction wrote nothing beyond (possibly) a no-op recommendation sync —
    the idempotence the tick process depends on."""

    model_config = ConfigDict(frozen=True)

    score: int
    components: Components
    changed: bool


class ScoreStore(Protocol):
    async def recompute(
        self,
        user_ref: UserRef,
        *,
        cause_kind: str,
        cause_ref: str | None = None,
        now: datetime | None = None,
    ) -> ScoreResult | None: ...

    async def get_score(self, user_ref: UserRef) -> dict[str, Any] | None: ...

    async def list_events(
        self, user_ref: UserRef, *, limit: int = 50
    ) -> list[dict[str, Any]]: ...

    async def all_subject_refs(self) -> tuple[UserRef, ...]: ...

    async def expire_due_threat_events(self, *, now: datetime) -> int: ...


class PostgresScoreStore:
    """The one writer. See the module docstring for the transaction shape."""

    def __init__(
        self, pool: AsyncConnectionPool, *, weights: ScoreWeights, config_version: str
    ) -> None:
        self._pool = pool
        self._weights = weights
        self._config_version = config_version

    async def recompute(
        self,
        user_ref: UserRef,
        *,
        cause_kind: str,
        cause_ref: str | None = None,
        now: datetime | None = None,
    ) -> ScoreResult | None:
        moment = now if now is not None else datetime.now(UTC)
        async with self._pool.connection() as conn, conn.transaction():
            # Step 1.
            cur = await conn.execute(_GET_SUBJECT_SQL, {"user_ref": user_ref})
            if await cur.fetchone() is None:
                return None

            # Step 2.
            cur = await conn.execute(_LOCK_SCORE_SQL, {"user_ref": user_ref})
            existing = await cur.fetchone()
            if existing is None:
                baseline_score = 0
                baseline = Components(posture=0, coverage=0, exposure=0, threat=0)
            else:
                baseline_score = existing[0]
                baseline = Components(**existing[1])

            # Step 3.
            state = await self._load_state(conn, user_ref, aged_open_recs=0)
            events_needing_scan = await self._load_events_needing_scan(conn, user_ref)

            # Step 4.
            specs = desired(state, events_needing_scan, self._weights)
            await self._sync_recommendations(conn, user_ref, specs, moment)
            aged_open_recs = await self._load_aged_open_recs(conn, user_ref)
            state = state.model_copy(update={"aged_open_recs": aged_open_recs})

            # Step 5.
            new_components = compute(state, self._weights)
            deltas = {
                name: getattr(new_components, name) - getattr(baseline, name)
                for name in _COMPONENT_ORDER
            }
            changed = any(delta != 0 for delta in deltas.values())
            if not changed:
                return ScoreResult(score=baseline_score, components=baseline, changed=False)

            running = baseline_score
            for component in _COMPONENT_ORDER:
                delta = deltas[component]
                if delta == 0:
                    continue
                running += delta
                await conn.execute(
                    _INSERT_SCORE_EVENT_SQL,
                    {
                        "user_ref": user_ref,
                        "delta": delta,
                        "component": component,
                        "cause_kind": cause_kind,
                        "cause_ref": cause_ref,
                        "config_version": self._config_version,
                        "score_after": running,
                    },
                )
            total = new_components.total
            assert running == total, "journal running total must equal the new score"

            # Step 6.
            await conn.execute(
                _UPSERT_SCORE_SQL,
                {
                    "user_ref": user_ref,
                    "score": total,
                    "components": Jsonb(new_components.as_dict()),
                    "config_version": self._config_version,
                },
            )
        # Step 7.
        return ScoreResult(score=total, components=new_components, changed=True)

    async def _load_state(
        self, conn: Any, user_ref: UserRef, *, aged_open_recs: int
    ) -> ScoreState:
        cur = await conn.execute(_ENROLMENT_ACTIVE_SQL, {"user_ref": user_ref})
        row = await cur.fetchone()
        assert row is not None
        enrolment_active = bool(row[0])

        cur = await conn.execute(
            _SEED_STATE_SQL,
            {
                "user_ref": user_ref,
                "fresh_days": self._weights.seed_fresh_days,
                "grace_days": self._weights.scan_grace_days,
            },
        )
        row = await cur.fetchone()
        assert row is not None
        seed_count, seeds_fresh, has_overdue_scan = row

        cur = await conn.execute(_MONITORED_SOURCES_SQL, {"user_ref": user_ref})
        row = await cur.fetchone()
        assert row is not None
        monitored_sources = row[0]

        cur = await conn.execute(_CONFIRMED_HITS_SQL, {"user_ref": user_ref})
        hit_rows = await cur.fetchall()
        confirmed_hits = tuple(
            ConfirmedHit(severity=severity, counts=bool(counts))
            for severity, counts, _no_feedback in hit_rows
        )
        awaiting_feedback_count = sum(
            1 for _severity, counts, no_feedback in hit_rows if counts and no_feedback
        )

        cur = await conn.execute(_THREATS_SQL, {"user_ref": user_ref})
        threat_rows = await cur.fetchall()
        threats = tuple(
            ThreatPenalty(
                penalty=penalty, age_days=age_days, decay_days=decay_days, is_global=is_global
            )
            for penalty, decay_days, is_global, age_days in threat_rows
        )

        return ScoreState(
            enrolment_active=enrolment_active,
            seed_count=seed_count,
            seeds_fresh=seeds_fresh,
            has_overdue_scan=has_overdue_scan,
            monitored_sources=monitored_sources,
            confirmed_hits=confirmed_hits,
            awaiting_feedback_count=awaiting_feedback_count,
            aged_open_recs=aged_open_recs,
            threats=threats,
        )

    async def _load_events_needing_scan(
        self, conn: Any, user_ref: UserRef
    ) -> tuple[EventNeedingScan, ...]:
        cur = await conn.execute(_EVENTS_NEEDING_SCAN_SQL, {"user_ref": user_ref})
        rows = await cur.fetchall()
        return tuple(
            EventNeedingScan(event_id=event_id, expires_at=expires_at)
            for event_id, expires_at in rows
        )

    async def _load_aged_open_recs(self, conn: Any, user_ref: UserRef) -> int:
        cur = await conn.execute(
            _AGED_OPEN_RECS_SQL,
            {"user_ref": user_ref, "soft_age_days": self._weights.rec_soft_age_days},
        )
        row = await cur.fetchone()
        assert row is not None
        count: int = row[0]
        return count

    async def _sync_recommendations(
        self, conn: Any, user_ref: UserRef, specs: Sequence[RecSpec], now: datetime
    ) -> None:
        # Expire first: an open recommendation past its expiry is neither
        # "still desired" nor "no longer desired" — it is stale regardless of
        # what the catalog says this instant.
        await conn.execute(_EXPIRE_RECS_SQL, {"user_ref": user_ref, "now": now})

        cur = await conn.execute(_OPEN_RECS_SQL, {"user_ref": user_ref})
        open_rows = await cur.fetchall()
        desired_keys = {(spec.kind, spec.source_event_id) for spec in specs}
        for rec_id, kind, source_event_id in open_rows:
            if (kind, source_event_id) not in desired_keys:
                await conn.execute(_COMPLETE_REC_SQL, {"rec_id": rec_id})

        for spec in specs:
            await conn.execute(
                _INSERT_REC_SQL,
                {
                    "user_ref": user_ref,
                    "kind": spec.kind,
                    "params": Jsonb(spec.params),
                    "source_event_id": spec.source_event_id,
                    "expires_at": spec.expires_at,
                },
            )

    async def get_score(self, user_ref: UserRef) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_GET_SCORE_SQL, {"user_ref": user_ref})
            row = await cur.fetchone()
        if row is None:
            return None
        score, components, config_version, computed_at = row
        return {
            "score": score,
            "components": components,
            "config_version": config_version,
            "computed_at": computed_at,
        }

    async def list_events(
        self, user_ref: UserRef, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_LIST_EVENTS_SQL, {"user_ref": user_ref, "limit": limit})
            rows = await cur.fetchall()
        return [
            {
                "score_event_id": row[0],
                "delta": row[1],
                "component": row[2],
                "cause_kind": row[3],
                "cause_ref": row[4],
                "config_version": row[5],
                "score_after": row[6],
                "created_at": row[7],
            }
            for row in rows
        ]

    async def all_subject_refs(self) -> tuple[UserRef, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_ALL_SUBJECTS_SQL)
            rows = await cur.fetchall()
        return tuple(parse_user_ref(row[0]) for row in rows)

    async def expire_due_threat_events(self, *, now: datetime) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_EXPIRE_DUE_THREAT_EVENTS_SQL, {"now": now})
            rowcount: int = cur.rowcount
            return rowcount


__all__ = ["PostgresScoreStore", "ScoreResult", "ScoreStore"]
