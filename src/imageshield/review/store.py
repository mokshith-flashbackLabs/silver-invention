"""The review queue store — the human-only confirm gate (migration 0021;
INVARIANTS #19: nothing reaches a user from the ``review`` band without a
human decision, and there is no timeout that auto-promotes).

``decide`` is this module's reason to exist and it is ONE transaction:

1. ``SELECT ... FROM review_tasks WHERE task_id = %s AND status = 'pending'
   FOR UPDATE`` — a task that does not exist, or is already ``decided`` (or
   ``quarantined``), answers ``None``. The lock means two operators racing on
   the same task cannot both "win".
2. ``decision == 'uncertain'`` is representable in the API but the 0021
   ``review_tasks.decision`` CHECK only allows ``'confirmed'`` /
   ``'rejected'`` — so an ``uncertain`` call writes nothing to the task or the
   infringement. Its entire record is the audit row. The task stays
   ``pending`` and the next ``next_task`` call returns it again; a reviewer
   who could not tell has told us something, but nothing here decides *for*
   them, and nothing times out to decide on their behalf.
3. Otherwise the task is marked ``decided`` and the infringement's
   ``confirm_state`` moves to ``confirmed``/``rejected``, with an optional
   severity override applied via ``COALESCE`` (so an omitted override leaves
   the machine-triaged severity untouched) and ``confirm_decided_by`` /
   ``confirm_decided_at`` set from the request's ``operator`` string — never
   the admin token constant, because the 0021
   ``infringements_confirmed_needs_human`` CHECK exists precisely so a
   ``confirmed`` row can never lack a named human.
4. An audit row records the decision either way. ``REVIEW_DECIDED_ACTION`` is
   one action name for both paths (step 2's audit row and step 4's), so a
   console reading ``audit_log`` for "what happened to this task" needs one
   filter, not two.

``next_task`` mirrors migration 0021's ``review_tasks_queue_idx`` ordering
(severity rank, then ``created_at``) exactly, joined out to the fields a
review console needs to render one card: ``infringements.image_url`` /
``page_url`` / ``face_match_score`` and ``content_urls.source_domain``. The
``triage`` jsonb rides along untouched — it is where ``best_face_bbox`` and
the rest of the machine's working notes live (CLAUDE.md §9: text about the
image, never pixels).
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict

from imageshield.types import UserRef, parse_user_ref

REVIEW_DECIDED_ACTION = "review.decided"
SUBJECT_DECIDED_ACTION = "review.subject_decided"

# 0021's severity vocabulary, in the same worst-to-least order as the
# review_tasks_queue_idx CASE expression — repeated here (rather than
# imported from confirm/triage.py) because this module's queue-depth shape
# is a review-console concern, independent of the triage classifier's.
_SEVERITIES: tuple[str, ...] = (
    "ncii_suspected",
    "explicit_unmatched",
    "unassessed",
    "benign_copy",
    "likely_not_subject",
)

_NEXT_TASK_SQL = """
    SELECT rt.task_id, rt.infringement_id, rt.user_ref, rt.severity, rt.triage,
           i.image_url, i.page_url, i.face_match_score, cu.source_domain
    FROM review_tasks rt
    JOIN infringements i ON i.infringement_id = rt.infringement_id
    JOIN content_urls cu ON cu.url_hash = i.url_hash
    WHERE rt.status = 'pending'
    ORDER BY
      CASE rt.severity
        WHEN 'ncii_suspected'     THEN 0
        WHEN 'explicit_unmatched' THEN 1
        WHEN 'unassessed'         THEN 2
        WHEN 'benign_copy'        THEN 3
        ELSE 4
      END,
      rt.created_at
    LIMIT 1
"""

_QUEUE_DEPTH_SQL = """
    SELECT severity, count(*)::int
    FROM review_tasks
    WHERE status = 'pending'
    GROUP BY severity
"""

# FOR UPDATE: the #19 moment. Two operators racing the same task must not
# both see it pending and both write a decision.
_LOCK_TASK_SQL = """
    SELECT task_id, infringement_id, user_ref
    FROM review_tasks
    WHERE task_id = %(task_id)s AND status = 'pending'
    FOR UPDATE
"""

_UPDATE_TASK_SQL = """
    UPDATE review_tasks
    SET status = 'decided', decision = %(decision)s, decided_by = %(operator)s,
        decided_at = now()
    WHERE task_id = %(task_id)s
"""

# RETURNING severity: the final value after COALESCE, read back rather than
# recomputed in Python, so the outcome can never disagree with what actually
# committed.
_UPDATE_INFRINGEMENT_SQL = """
    UPDATE infringements
    SET confirm_state = %(confirm_state)s,
        severity = COALESCE(%(severity_override)s, severity),
        confirm_decided_by = %(operator)s,
        confirm_decided_at = now()
    WHERE infringement_id = %(infringement_id)s
    RETURNING severity
"""

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, subject_ref, resource_id, metadata)
    VALUES ('operator', %(action)s, %(subject_ref)s, %(resource_id)s, %(metadata)s)
"""

# ── the subject's own decision (spec 2026-08-21 §5) ──────────────────────────
#
# ``user_ref`` in the WHERE, same 404-oracle discipline as feedback: absent and
# not-yours are one indistinguishable None. quarantined/duplicate are filtered
# in Python off the locked row — to the subject those rows do not exist either.
_LOCK_INFRINGEMENT_SUBJECT_SQL = """
    SELECT i.confirm_state, i.confirm_decided_by, i.severity, cu.source_domain
    FROM infringements i
    JOIN content_urls cu ON cu.url_hash = i.url_hash
    WHERE i.infringement_id = %(infringement_id)s AND i.user_ref = %(user_ref)s
    FOR UPDATE OF i
"""

# severity is deliberately untouched: it stays whatever machine triage
# assigned. 'rejected' also retires the hit from the user's own counts via the
# same status value the not_me feedback signal uses.
#
# THE SECOND BRANCH IS WHAT MAKES A REVERSAL COMPLETE. Rejecting sets
# status='dismissed_not_me'; un-rejecting has to take it off again, or the hit
# comes back as confirm_state='confirmed' while still carrying the dismissal
# that hides it from live exposure and from the weekly report's countable hits
# (the proxy's reports/close.ts filters exactly that value). The user would tap
# "actually this is me", see the card change, and the number would not move.
#
# It clears to 'new', not to 'acknowledged', for two reasons. 'new' is where a
# FIRST-TIME confirm leaves it, so decide(X) lands in the same place however
# many answers came before — the property worth having. And 'acknowledged'
# belongs to the feedback lane, which is a different axis: this decision says
# "that is me", not "I am reporting it as abuse".
#
# Only 'dismissed_not_me' is cleared. 'authorised', 'user_resolved' and
# 'acknowledged' were set through the feedback lane and are not ours to undo.
_SUBJECT_DECIDE_INFRINGEMENT_SQL = """
    UPDATE infringements
    SET confirm_state = %(confirm_state)s,
        confirm_decided_by = 'subject',
        confirm_decided_at = now(),
        status = CASE
                   WHEN %(confirm_state)s = 'rejected' THEN 'dismissed_not_me'
                   WHEN status = 'dismissed_not_me' THEN 'new'
                   ELSE status
                 END
    WHERE infringement_id = %(infringement_id)s
    RETURNING severity
"""

# 'pending' OR a task this same subject already decided. Without the second
# arm a reversal updates zero rows and the queue keeps asserting the answer the
# user just changed — infringements says 'confirmed', review_tasks still says
# 'rejected', and the reviewer-facing side quietly disagrees with the product.
#
# `decided_by = 'subject'` carries the never-overturn-an-operator rule down to
# the task row, so it holds even if a caller reaches this SQL by another path.
_SUBJECT_DECIDE_TASK_SQL = """
    UPDATE review_tasks
    SET status = 'decided', decision = %(decision)s,
        decided_by = 'subject', decided_at = now()
    WHERE infringement_id = %(infringement_id)s
      AND (status = 'pending'
           OR (status = 'decided' AND decided_by = 'subject'))
"""

_SUBJECT_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, subject_ref, resource_id, metadata)
    VALUES ('subject', %(action)s, %(subject_ref)s, %(resource_id)s, %(metadata)s)
"""

# The console observer feed (spec 2026-08-21 §6): what subjects decided, off
# the denormalised audit metadata — one read, no joins. An operator page with
# tiny N; no index until it hurts.
_SUBJECT_DECISIONS_SQL = """
    SELECT occurred_at, subject_ref, resource_id, metadata
    FROM audit_log
    WHERE action = 'review.subject_decided'
    ORDER BY occurred_at DESC
    LIMIT %(limit)s
"""

# The control room always sees THAT a person has a hit (owner requirement,
# 2026-08-21) — what it never sees is the hit's pixels. Every hit still
# awaiting an answer, metadata only.
_OPEN_HITS_SQL = """
    SELECT i.user_ref, i.infringement_id, i.confirm_state, i.severity,
           cu.source_domain, i.first_seen_at
    FROM infringements i
    JOIN content_urls cu ON cu.url_hash = i.url_hash
    WHERE i.confirm_state IN ('unconfirmed', 'machine_triaged')
    ORDER BY i.first_seen_at DESC
    LIMIT %(limit)s
"""


class DecisionOutcome(BaseModel):
    """What one ``decide`` call produced. For ``uncertain``, ``severity`` is
    always ``None`` — nothing was written to the infringement, so there is no
    final severity to report."""

    model_config = ConfigDict(frozen=True)

    infringement_id: UUID
    user_ref: UserRef
    decision: str
    severity: str | None


class SubjectDecisionOutcome(BaseModel):
    """What one ``subject_decide`` call produced. ``outcome`` is the route's
    dispatch key: ``decided`` wrote the transition (including a subject
    reversing their own earlier answer, since 2026-09-01), ``replay`` found the
    identical subject decision already committed (idempotent no-op), and
    ``conflict`` now means one thing only — an OPERATOR decided this hit, and a
    subject may not overturn that (409)."""

    model_config = ConfigDict(frozen=True)

    infringement_id: UUID
    decision: str
    severity: str | None
    outcome: str  # 'decided' | 'replay' | 'conflict'


class ReviewStore(Protocol):
    async def next_task(self) -> dict[str, Any] | None: ...

    async def queue_depth(self) -> dict[str, int]: ...

    async def decide(
        self, task_id: UUID, *, decision: str, operator: str, severity: str | None
    ) -> DecisionOutcome | None: ...

    async def subject_decide(
        self, infringement_id: UUID, *, user_ref: UserRef, decision: str
    ) -> SubjectDecisionOutcome | None: ...

    async def subject_decisions(self, *, limit: int) -> tuple[dict[str, Any], ...]: ...

    async def open_hits(self, *, limit: int) -> tuple[dict[str, Any], ...]: ...


class PostgresReviewStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def next_task(self) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_NEXT_TASK_SQL)
            row = await cur.fetchone()
        if row is None:
            return None
        (
            task_id,
            infringement_id,
            user_ref,
            severity,
            triage,
            image_url,
            page_url,
            face_match_score,
            source_domain,
        ) = row
        return {
            "task_id": task_id,
            "infringement_id": infringement_id,
            "user_ref": parse_user_ref(user_ref),
            "severity": severity,
            "triage": triage,
            "image_url": image_url,
            "page_url": page_url,
            "face_match_score": (
                float(face_match_score) if face_match_score is not None else None
            ),
            "source_domain": source_domain,
        }

    async def queue_depth(self) -> dict[str, int]:
        depths: dict[str, int] = dict.fromkeys(_SEVERITIES, 0)
        async with self._pool.connection() as conn:
            cur = await conn.execute(_QUEUE_DEPTH_SQL)
            rows = await cur.fetchall()
        for severity, count in rows:
            depths[severity] = count
        return depths

    async def decide(
        self, task_id: UUID, *, decision: str, operator: str, severity: str | None
    ) -> DecisionOutcome | None:
        async with self._pool.connection() as conn, conn.transaction():
            # Step 1.
            cur = await conn.execute(_LOCK_TASK_SQL, {"task_id": task_id})
            row = await cur.fetchone()
            if row is None:
                return None
            _task_id, infringement_id, raw_user_ref = row
            user_ref = parse_user_ref(raw_user_ref)

            # Step 2.
            if decision == "uncertain":
                await conn.execute(
                    _AUDIT_SQL,
                    {
                        "action": REVIEW_DECIDED_ACTION,
                        "subject_ref": user_ref,
                        "resource_id": infringement_id,
                        "metadata": Jsonb({"decision": "uncertain", "operator": operator}),
                    },
                )
                return DecisionOutcome(
                    infringement_id=infringement_id,
                    user_ref=user_ref,
                    decision="uncertain",
                    severity=None,
                )

            # Step 3.
            confirm_state = "confirmed" if decision == "confirmed" else "rejected"
            await conn.execute(
                _UPDATE_TASK_SQL,
                {"task_id": task_id, "decision": decision, "operator": operator},
            )
            cur = await conn.execute(
                _UPDATE_INFRINGEMENT_SQL,
                {
                    "infringement_id": infringement_id,
                    "confirm_state": confirm_state,
                    "severity_override": severity,
                    "operator": operator,
                },
            )
            infr_row = await cur.fetchone()
            assert infr_row is not None
            final_severity: str | None = infr_row[0]

            # Step 4.
            await conn.execute(
                _AUDIT_SQL,
                {
                    "action": REVIEW_DECIDED_ACTION,
                    "subject_ref": user_ref,
                    "resource_id": infringement_id,
                    "metadata": Jsonb(
                        {"decision": decision, "severity": final_severity, "operator": operator}
                    ),
                },
            )
        return DecisionOutcome(
            infringement_id=infringement_id,
            user_ref=user_ref,
            decision=decision,
            severity=final_severity,
        )

    async def subject_decide(
        self, infringement_id: UUID, *, user_ref: UserRef, decision: str
    ) -> SubjectDecisionOutcome | None:
        """The subject's own confirm/reject — one transaction, mirroring
        ``decide``. Spec 2026-08-21 §0.1: the subject is a valid deciding
        human (INVARIANTS #19 as amended); the 0021 CHECK is satisfied by
        ``confirm_decided_by = 'subject'``.

        **A subject can never overturn an operator** — that is the part of the
        old rule that stands. What changed on 2026-09-01 is the other part: a
        subject MAY overturn their own earlier answer, as often as they like.
        Someone who tapped "not me" on a real hit needs a way back that is not
        a support ticket, and ``feedback.py`` already spells out why: users
        reject true positives under distress, and it is common."""
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _LOCK_INFRINGEMENT_SUBJECT_SQL,
                {"infringement_id": infringement_id, "user_ref": user_ref},
            )
            row = await cur.fetchone()
            if row is None:
                return None
            confirm_state, decided_by, severity, source_domain = row
            if confirm_state in ("quarantined", "duplicate"):
                # To the subject these rows do not exist — same answer as
                # absent, so the response can never confirm a quarantined hit.
                return None
            if confirm_state in ("confirmed", "rejected"):
                if decided_by != "subject":
                    # AN OPERATOR DECIDED THIS. A subject can never overturn
                    # one, and that half of the rule is unchanged.
                    return SubjectDecisionOutcome(
                        infringement_id=infringement_id,
                        decision=confirm_state,
                        severity=severity,
                        outcome="conflict",
                    )
                if confirm_state == decision:
                    return SubjectDecisionOutcome(
                        infringement_id=infringement_id,
                        decision=decision,
                        severity=severity,
                        outcome="replay",
                    )
                # THE SUBJECT IS CHANGING THEIR OWN MIND, and since 2026-09-01
                # that is allowed — it falls through to the same write below.
                #
                # It used to be a 409: "v1 has no re-decide, changes go through
                # the team". That was wrong in the one direction that matters.
                # A person who taps "not me" on a real hit -- under distress,
                # by mistake, or before recognising the photo -- had no way
                # back except contacting support, and feedback.py already
                # records why that misreads people: users reject TRUE positives
                # and it is common. The row was never deleted and the report
                # never disappeared, so nothing had to be recovered; only the
                # answer was frozen.
                #
                # Nothing here is destructive. infringement_feedback is
                # append-only, the audit row below is a second row rather than
                # an edit, and the reversal is as visible in the record as the
                # original answer was.

            cur = await conn.execute(
                _SUBJECT_DECIDE_INFRINGEMENT_SQL,
                {"infringement_id": infringement_id, "confirm_state": decision},
            )
            infr_row = await cur.fetchone()
            assert infr_row is not None
            final_severity: str | None = infr_row[0]
            await conn.execute(
                _SUBJECT_DECIDE_TASK_SQL,
                {"infringement_id": infringement_id, "decision": decision},
            )
            await conn.execute(
                _SUBJECT_AUDIT_SQL,
                {
                    "action": SUBJECT_DECIDED_ACTION,
                    "subject_ref": user_ref,
                    "resource_id": infringement_id,
                    # source_domain denormalised in so the console observer
                    # feed is one indexed read of audit_log, no joins.
                    "metadata": Jsonb(
                        {
                            "decision": decision,
                            "severity": final_severity,
                            "source_domain": source_domain,
                        }
                    ),
                },
            )
        return SubjectDecisionOutcome(
            infringement_id=infringement_id,
            decision=decision,
            severity=final_severity,
            outcome="decided",
        )

    async def subject_decisions(self, *, limit: int) -> tuple[dict[str, Any], ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_SUBJECT_DECISIONS_SQL, {"limit": limit})
            rows = await cur.fetchall()
        decisions = []
        for occurred_at, subject_ref, resource_id, metadata in rows:
            meta = metadata if isinstance(metadata, dict) else {}
            decisions.append(
                {
                    "occurred_at": occurred_at,
                    "user_ref": parse_user_ref(subject_ref),
                    "infringement_id": resource_id,
                    "decision": meta.get("decision"),
                    "severity": meta.get("severity"),
                    "source_domain": meta.get("source_domain"),
                }
            )
        return tuple(decisions)

    async def open_hits(self, *, limit: int) -> tuple[dict[str, Any], ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_OPEN_HITS_SQL, {"limit": limit})
            rows = await cur.fetchall()
        return tuple(
            {
                "user_ref": parse_user_ref(user_ref),
                "infringement_id": infringement_id,
                "confirm_state": confirm_state,
                "severity": severity,
                "source_domain": source_domain,
                "first_seen_at": first_seen_at,
            }
            for (
                user_ref,
                infringement_id,
                confirm_state,
                severity,
                source_domain,
                first_seen_at,
            ) in rows
        )


__all__ = [
    "REVIEW_DECIDED_ACTION",
    "SUBJECT_DECIDED_ACTION",
    "DecisionOutcome",
    "PostgresReviewStore",
    "ReviewStore",
    "SubjectDecisionOutcome",
]
