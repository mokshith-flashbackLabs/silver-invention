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

``list_hits`` / ``record_verdict`` / ``verdict_stats`` (2026-09-14) are the
reviewer-feed half and they are deliberately BESIDE ``decide``, not inside it.
A verdict is a **label**: "was the machine right about this hit", which is a
different question from "is this hit infringing". It never writes
``infringements`` and never writes ``review_tasks`` — owner decision D6 — so
``decide`` stays the only override path, and two reviewers may label the same
hit without racing each other. The reason the table exists at all is that face
matching runs on Rekognition today, the team is replacing it, and a
false-positive rate cannot be measured without a human recording one.

``next_task`` mirrors migration 0021's ``review_tasks_queue_idx`` ordering
(severity rank, then ``created_at``) exactly, joined out to the fields a
review console needs to render one card: ``infringements.image_url`` /
``page_url`` / ``face_match_score`` and ``content_urls.source_domain``. The
``triage`` jsonb rides along untouched — it is where ``best_face_bbox`` and
the rest of the machine's working notes live (CLAUDE.md §9: text about the
image, never pixels).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict

from imageshield.types import UserRef, parse_user_ref

REVIEW_DECIDED_ACTION = "review.decided"
SUBJECT_DECIDED_ACTION = "review.subject_decided"
# A verdict is its own action, never REVIEW_DECIDED_ACTION: a console
# reading "what happened to this hit" must be able to tell a label from a
# decision with one filter, because only one of the two moved any state.
REVIEW_VERDICT_ACTION = "review.verdict_recorded"

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


# ── the reviewer hit feed and its verdicts (2026-09-14) ──────────────────
#
# THE REPRESENTATIVE-ATTESTATION PICK IS COPIED FROM confirm/store.py's
# _LOAD_CONTEXT_SQL, not re-derived: band rank, then provider_score, then
# provider_id as the tiebreak. The same ordering is the `representative` CTE
# in svc.v_person_hits (0016 onward). A LATERAL rather than a CTE because this
# query has a LIMIT -- the CTE form materialises a pick for every infringement
# in the table in order to return fifty rows.
#
# `preview_available` is 0031's expression, verbatim. It answers "can
# GET /v1/admin/infringements/{id}/preview actually render this", and there is
# exactly one definition of it in this system (0031's comment says why); a
# second one here that drifted would put an operator in front of a hit whose
# preview then 404s.
#
# `confirm_state <> 'quarantined'` is NOT a filter the caller can turn off. A
# quarantined hit is CSAM-suspected; it is excluded from every svc view, from
# the subject's surface, and from here. Escalation out of a quarantine is the
# manual legal process in docs/OPERATIONS.md, not a listing.
#
# The keyset is (first_seen_at, infringement_id) DESC, matching 0033's
# infringements_feed_keyset_idx. A row-tuple comparison rather than
# `first_seen_at < x OR (= x AND id < y)`: one expression the planner can use
# as a range scan, and one that cannot be got subtly wrong.
_LIST_HITS_SQL = """
    SELECT i.infringement_id, i.user_ref, cu.source_domain, i.page_url, i.image_url,
           i.first_seen_at, i.status, i.confirm_state, i.severity,
           i.confirm_decided_by, i.confirm_decided_at, i.face_match_score,
           i.moderation_labels, i.duplicate_of,
           (
             COALESCE(i.preview_image_url, i.image_url) IS NOT NULL
             AND jsonb_typeof(rt.triage -> 'best_face_bbox') = 'object'
           ) AS preview_available,
           rt.task_id, rt.status, rt.decision, rt.decided_by, rt.decided_at, rt.severity,
           lv.verdict_id, lv.operator, lv.verdict, lv.note, lv.created_at,
           seed.source_object_ref, seed.seed_kind
    FROM infringements i
    JOIN content_urls cu ON cu.url_hash = i.url_hash
    LEFT JOIN review_tasks rt ON rt.infringement_id = i.infringement_id
    LEFT JOIN LATERAL (
        SELECT v.verdict_id, v.operator, v.verdict, v.note, v.created_at
        FROM review_verdicts v
        WHERE v.infringement_id = i.infringement_id
        ORDER BY v.created_at DESC, v.verdict_id DESC
        LIMIT 1
    ) lv ON true
    LEFT JOIN LATERAL (
        SELECT a.last_run_id
        FROM attestations a
        WHERE a.infringement_id = i.infringement_id
        ORDER BY
            CASE a.band WHEN 'auto_confirm' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
            a.provider_score DESC NULLS LAST,
            a.provider_id
        LIMIT 1
    ) rep ON true
    LEFT JOIN search_runs run ON run.run_id = rep.last_run_id
    LEFT JOIN search_seeds seed ON seed.seed_id = run.seed_id
    WHERE i.confirm_state <> 'quarantined'
      AND (%(severity)s::text IS NULL OR i.severity = %(severity)s)
      AND (%(confirm_state)s::text IS NULL OR i.confirm_state = %(confirm_state)s)
      AND (%(user_ref)s::uuid IS NULL OR i.user_ref = %(user_ref)s)
      AND (%(since)s::timestamptz IS NULL OR i.first_seen_at >= %(since)s)
      AND (%(after_ts)s::timestamptz IS NULL
           OR (i.first_seen_at, i.infringement_id)
               < (%(after_ts)s::timestamptz, %(after_id)s::uuid))
    ORDER BY i.first_seen_at DESC, i.infringement_id DESC
    LIMIT %(limit)s
"""

# The snapshot columns come from the hit AT THE MOMENT OF THE VERDICT, read in
# the same transaction as the INSERT below. Joining them at read time instead
# would measure whatever an operator later overrode, not what the machine did.
# No FOR UPDATE: nothing here is a read-modify-write of the infringement, and
# taking a write lock on a row this method never writes would let a label
# block a decision.
_VERDICT_TARGET_SQL = """
    SELECT i.user_ref, i.severity, i.face_match_score, i.confirm_state
    FROM infringements i
    WHERE i.infringement_id = %(infringement_id)s
"""

_INSERT_VERDICT_SQL = """
    INSERT INTO review_verdicts
      (infringement_id, operator, verdict, note, machine_severity,
       face_match_score, confirm_state_at_verdict)
    VALUES (%(infringement_id)s, %(operator)s, %(verdict)s, %(note)s,
            %(machine_severity)s, %(face_match_score)s, %(confirm_state)s)
    RETURNING verdict_id, created_at
"""

# LATEST VERDICT PER HIT, not every verdict. A reviewer who labels a hit
# false_positive and then, on a second look, true_positive has changed their
# mind once -- counting both would let one hit contribute twice to a rate that
# is supposed to describe the machine.
#
# fp / (tp + fp). `unsure` is in the totals and in NEITHER side of the rate: a
# reviewer who could not tell has told us something, and it is not "the
# machine was wrong". NULLIF makes a zero denominator a SQL NULL, which
# travels as None -- never a fabricated 0.0, which would read as "no false
# positives" out of a window where nobody decided anything.
_VERDICT_BY_SEVERITY_SQL = """
    WITH latest AS (
        SELECT DISTINCT ON (v.infringement_id)
               v.infringement_id, v.verdict, v.machine_severity
        FROM review_verdicts v
        WHERE v.created_at >= %(since)s
        ORDER BY v.infringement_id, v.created_at DESC, v.verdict_id DESC
    )
    SELECT machine_severity,
           count(*)::int AS total,
           count(*) FILTER (WHERE verdict = 'true_positive')::int AS true_positive,
           count(*) FILTER (WHERE verdict = 'false_positive')::int AS false_positive,
           count(*) FILTER (WHERE verdict = 'unsure')::int AS unsure,
           (count(*) FILTER (WHERE verdict = 'false_positive')::float
            / NULLIF(count(*) FILTER (WHERE verdict IN
                ('true_positive', 'false_positive')), 0)) AS false_positive_rate
    FROM latest
    GROUP BY machine_severity
    ORDER BY machine_severity
"""

# Agreement between the person in the photo and the reviewer looking at it.
# Only hits the SUBJECT decided (confirm_decided_by = 'subject') are
# comparable -- an operator-decided hit compares a reviewer to a reviewer.
# `unsure` is EXCLUDED from the comparison rather than counted as
# disagreement: it is the absence of an opinion, and scoring it as a
# disagreement would make a cautious reviewer look wrong.
_VERDICT_SUBJECT_AGREEMENT_SQL = """
    WITH latest AS (
        SELECT DISTINCT ON (v.infringement_id)
               v.infringement_id, v.verdict
        FROM review_verdicts v
        WHERE v.created_at >= %(since)s
        ORDER BY v.infringement_id, v.created_at DESC, v.verdict_id DESC
    )
    SELECT count(*)::int AS compared,
           count(*) FILTER (
             WHERE (i.confirm_state = 'confirmed' AND l.verdict = 'true_positive')
                OR (i.confirm_state = 'rejected' AND l.verdict = 'false_positive')
           )::int AS agreed
    FROM latest l
    JOIN infringements i ON i.infringement_id = l.infringement_id
    WHERE i.confirm_decided_by = 'subject'
      AND l.verdict <> 'unsure'
"""

# EVERY verdict row in the window, not the latest per hit: this is throughput
# per person, and a reviewer who looked twice did the work twice.
_VERDICT_BY_OPERATOR_SQL = """
    SELECT operator,
           count(*)::int AS total,
           count(*) FILTER (WHERE verdict = 'true_positive')::int AS true_positive,
           count(*) FILTER (WHERE verdict = 'false_positive')::int AS false_positive,
           count(*) FILTER (WHERE verdict = 'unsure')::int AS unsure
    FROM review_verdicts
    WHERE created_at >= %(since)s
    GROUP BY operator
    ORDER BY operator
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


def _hit_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """One `_LIST_HITS_SQL` row into the feed's shape.

    ``moderation_labels`` is reduced to label NAMES here rather than shipped
    whole: the stored JSONB carries per-label confidences, which are the
    classifier's working notes and not something a reviewer reads. Names are
    text about the image (INVARIANTS #9), and so were the confidences — this
    is an altitude choice, not a safety one.
    """
    (
        infringement_id,
        user_ref,
        source_domain,
        page_url,
        image_url,
        first_seen_at,
        status,
        confirm_state,
        severity,
        confirm_decided_by,
        confirm_decided_at,
        face_match_score,
        moderation_labels,
        duplicate_of,
        preview_available,
        task_id,
        task_status,
        task_decision,
        task_decided_by,
        task_decided_at,
        task_severity,
        verdict_id,
        verdict_operator,
        verdict_value,
        verdict_note,
        verdict_created_at,
        source_object_ref,
        seed_kind,
    ) = row
    labels: list[str] = []
    if isinstance(moderation_labels, list):
        labels = [
            str(label["name"])
            for label in moderation_labels
            if isinstance(label, dict) and label.get("name")
        ]
    return {
        "infringement_id": infringement_id,
        "user_ref": parse_user_ref(user_ref),
        "source_domain": source_domain,
        "page_url": page_url,
        "image_url": image_url,
        "first_seen_at": first_seen_at,
        "status": status,
        "confirm_state": confirm_state,
        "severity": severity,
        "confirm_decided_by": confirm_decided_by,
        "confirm_decided_at": confirm_decided_at,
        "face_match_score": (
            float(face_match_score) if face_match_score is not None else None
        ),
        "moderation_labels": labels,
        "duplicate_of": duplicate_of,
        "preview_available": bool(preview_available),
        "review_task": (
            None
            if task_id is None
            else {
                "task_id": task_id,
                "status": task_status,
                "decision": task_decision,
                "decided_by": task_decided_by,
                "decided_at": task_decided_at,
                "severity": task_severity,
            }
        ),
        # Present IFF the subject decided it. `confirm_decided_by` is the one
        # column that distinguishes the three deciders ('subject', an
        # operator's name, or the 'auto:nsfw' machine marker), so a reviewer
        # reading this feed can tell whose answer they are looking at.
        "subject_decision": (
            {"decision": confirm_state, "decided_at": confirm_decided_at}
            if confirm_decided_by == "subject"
            else None
        ),
        "latest_verdict": (
            None
            if verdict_id is None
            else {
                "verdict_id": verdict_id,
                "operator": verdict_operator,
                "verdict": verdict_value,
                "note": verdict_note,
                "created_at": verdict_created_at,
            }
        ),
        "source_object_ref": source_object_ref,
        "seed_kind": seed_kind,
    }


class VerdictRecord(BaseModel):
    """One reviewer label, as written. The three snapshot fields are the hit
    as it stood at the moment of the verdict — see ``_VERDICT_TARGET_SQL``."""

    model_config = ConfigDict(frozen=True)

    verdict_id: UUID
    infringement_id: UUID
    operator: str
    verdict: str
    note: str | None
    machine_severity: str | None
    face_match_score: float | None
    confirm_state_at_verdict: str
    created_at: datetime


class HitsPage(BaseModel):
    """One page of the reviewer feed. ``has_more`` is the store's answer, not
    the route's arithmetic: the store asked for ``limit + 1`` rows and knows
    whether the extra one came back."""

    model_config = ConfigDict(frozen=True)

    hits: tuple[dict[str, Any], ...]
    has_more: bool


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

    async def list_hits(
        self,
        *,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
        severity: str | None = None,
        confirm_state: str | None = None,
        user_ref: UserRef | None = None,
        since: datetime | None = None,
    ) -> HitsPage: ...

    async def record_verdict(
        self,
        infringement_id: UUID,
        *,
        operator: str,
        verdict: str,
        note: str | None,
    ) -> VerdictRecord | None: ...

    async def verdict_stats(self, *, since: datetime) -> dict[str, Any]: ...


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
        """The operator's decision — the module docstring has the transaction.

        **An AUTO-CONFIRMED hit reaches here through the same door.**
        ``record_auto_confirmed`` (2026-09-14) leaves the hit ``confirmed``
        while leaving its ``review_tasks`` row ``pending``, precisely so this
        method still finds a lockable task: ``decide`` with ``rejected`` is
        the override lane for a machine confirm, and it overwrites both the
        state and ``confirm_decided_by``, replacing the ``'auto:nsfw'`` marker
        with the operator's name. INVARIANTS #19/#47 rest on that being true;
        ``tests/test_review.py::test_an_operator_can_reject_an_auto_confirmed_hit``
        proves it.
        """
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

    # ── the reviewer feed and its verdicts (2026-09-14) ───────────────────

    async def list_hits(
        self,
        *,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
        severity: str | None = None,
        confirm_state: str | None = None,
        user_ref: UserRef | None = None,
        since: datetime | None = None,
    ) -> HitsPage:
        """Every hit a reviewer may look at, newest first, keyset-paged.

        ``limit + 1`` rows are fetched and the extra one is dropped: that is
        how ``has_more`` is known without a second ``count(*)`` over a table
        whose size is the reason this endpoint is paged at all.
        """
        params = {
            "limit": limit + 1,
            "after_ts": after[0] if after else None,
            "after_id": after[1] if after else None,
            "severity": severity,
            "confirm_state": confirm_state,
            "user_ref": user_ref,
            "since": since,
        }
        async with self._pool.connection() as conn:
            cur = await conn.execute(_LIST_HITS_SQL, params)
            rows = await cur.fetchall()
        has_more = len(rows) > limit
        return HitsPage(
            hits=tuple(_hit_row(row) for row in rows[:limit]), has_more=has_more
        )

    async def record_verdict(
        self,
        infringement_id: UUID,
        *,
        operator: str,
        verdict: str,
        note: str | None,
    ) -> VerdictRecord | None:
        """One transaction: read the hit, INSERT the label, audit it.

        **It writes NOTHING to ``infringements`` and NOTHING to
        ``review_tasks``** (owner decision D6). A verdict answers "was the
        machine right", which is a different question from "is this hit
        infringing" — the second is ``decide``'s and stays ``decide``'s.
        ``tests/test_review.py`` asserts both rows are byte-identical either
        side of a verdict.

        A quarantined hit answers ``None``, the same as an absent one: it is
        excluded from every surface, so there is nothing here to label.
        """
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _VERDICT_TARGET_SQL, {"infringement_id": infringement_id}
            )
            row = await cur.fetchone()
            if row is None:
                return None
            raw_user_ref, machine_severity, face_match_score, confirm_state = row
            if confirm_state == "quarantined":
                return None
            cur = await conn.execute(
                _INSERT_VERDICT_SQL,
                {
                    "infringement_id": infringement_id,
                    "operator": operator,
                    "verdict": verdict,
                    "note": note,
                    "machine_severity": machine_severity,
                    "face_match_score": face_match_score,
                    "confirm_state": confirm_state,
                },
            )
            inserted = await cur.fetchone()
            assert inserted is not None
            verdict_id, created_at = inserted
            await conn.execute(
                _AUDIT_SQL,
                {
                    "action": REVIEW_VERDICT_ACTION,
                    "subject_ref": parse_user_ref(raw_user_ref),
                    "resource_id": infringement_id,
                    "metadata": Jsonb(
                        {
                            "verdict": verdict,
                            "operator": operator,
                            "machine_severity": machine_severity,
                            "confirm_state": confirm_state,
                        }
                    ),
                },
            )
        return VerdictRecord(
            verdict_id=verdict_id,
            infringement_id=infringement_id,
            operator=operator,
            verdict=verdict,
            note=note,
            machine_severity=machine_severity,
            face_match_score=(
                float(face_match_score) if face_match_score is not None else None
            ),
            confirm_state_at_verdict=confirm_state,
            created_at=created_at,
        )

    async def verdict_stats(self, *, since: datetime) -> dict[str, Any]:
        """The measurement this whole surface exists to produce.

        Three groupings, three queries, one window. Rates are floats and never
        fabricated: a zero denominator is ``None``, because "no data" and "no
        false positives" are different claims and only one of them is safe to
        put in front of a decision about replacing the matcher.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(_VERDICT_BY_SEVERITY_SQL, {"since": since})
            severity_rows = await cur.fetchall()
            cur = await conn.execute(_VERDICT_SUBJECT_AGREEMENT_SQL, {"since": since})
            agreement_row = await cur.fetchone()
            cur = await conn.execute(_VERDICT_BY_OPERATOR_SQL, {"since": since})
            operator_rows = await cur.fetchall()
        assert agreement_row is not None
        compared, agreed = agreement_row
        return {
            "since": since,
            "by_severity": [
                {
                    "machine_severity": machine_severity,
                    "total": total,
                    "true_positive": true_positive,
                    "false_positive": false_positive,
                    "unsure": unsure,
                    "false_positive_rate": rate,
                }
                for (
                    machine_severity,
                    total,
                    true_positive,
                    false_positive,
                    unsure,
                    rate,
                ) in severity_rows
            ],
            "subject_agreement": {
                "compared": compared,
                "agreed": agreed,
                "disagreed": compared - agreed,
                # None, not 0.0: nothing was compared, so nothing agreed and
                # nothing disagreed either.
                "agreement_rate": (agreed / compared) if compared else None,
            },
            "by_operator": [
                {
                    "operator": operator,
                    "total": total,
                    "true_positive": true_positive,
                    "false_positive": false_positive,
                    "unsure": unsure,
                }
                for (
                    operator,
                    total,
                    true_positive,
                    false_positive,
                    unsure,
                ) in operator_rows
            ],
        }


__all__ = [
    "REVIEW_DECIDED_ACTION",
    "REVIEW_VERDICT_ACTION",
    "SUBJECT_DECIDED_ACTION",
    "DecisionOutcome",
    "HitsPage",
    "PostgresReviewStore",
    "ReviewStore",
    "SubjectDecisionOutcome",
    "VerdictRecord",
]
