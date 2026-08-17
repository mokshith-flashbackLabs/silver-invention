"""The expected shape of the four `svc` contract views.

This is the ONE place outside migration 0016 that names `svc.v_person_*`, which
is what the deploy checklist's `grep svc.v_person_` check relies on.

Why a shape check and not just an existence check: the views are a versioned
cross-repo contract (CLAUDE.md §3), and the tightest coupling in the
architecture. The proxy's own views JOIN against ours, so dropping or retyping a
column breaks the proxy at runtime while breaking nothing here — the failure
lands in the repo that did not cause it. Asserting the shape at readiness moves
that failure back to the deploy that caused it.

Columns may be ADDED freely, so the check is expected-subset-of-actual rather
than equality. Removals and retypes are the breaking changes and both are
caught.

Types are `information_schema.columns.data_type` spellings, taken from
migration 0016.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg import AsyncConnection

SVC_SCHEMA = "svc"

EXPECTED_VIEWS: dict[str, dict[str, str]] = {
    "v_person_enrolment_state": {
        "person_ref": "uuid",
        "status": "text",
        "model_id": "text",
        "enrolled_at": "timestamp with time zone",
    },
    "v_person_report_summary": {
        "person_ref": "uuid",
        "active_reports": "integer",
        "unresolved_matches": "integer",
        "live_exposure_count": "integer",
        "last_run_at": "timestamp with time zone",
        "first_scan_completed_at": "timestamp with time zone",
        "monitored_sources": "integer",
    },
    "v_person_hits": {
        "hit_id": "uuid",
        "report_id": "uuid",
        "person_ref": "uuid",
        "source_photo_id": "text",
        "hit_status": "text",
        "last_checked_at": "timestamp with time zone",
        "match_id": "uuid",
        "source_domain": "text",
        "host_page_url": "text",
        "face_bbox": "jsonb",
        "title": "text",
        "detected_at": "timestamp with time zone",
        "match_status": "text",
        "match_action": "text",
        "match_lifecycle": "text",
        "resolved_at": "timestamp with time zone",
        "resolution_note": "text",
        "provider_count": "integer",
        "score": "numeric",
    },
    "v_person_liveness_attempts": {
        "person_ref": "uuid",
        "attempts_24h": "integer",
        "last_attempt_at": "timestamp with time zone",
    },
}

_ACTUAL_SQL = """
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = %s
"""


async def check_svc_contract(conn: AsyncConnection[Any]) -> list[str]:
    """Return one message per contract violation; empty list means healthy.

    Messages name the view and column because this endpoint is internal and on a
    private subnet — a readiness probe that says only "not ready" costs an hour
    of digging.
    """
    actual: dict[str, dict[str, str]] = {}
    async with conn.cursor() as cur:
        await cur.execute(_ACTUAL_SQL, (SVC_SCHEMA,))
        for table_name, column_name, data_type in await cur.fetchall():
            actual.setdefault(table_name, {})[column_name] = data_type

    problems: list[str] = []
    for view, columns in EXPECTED_VIEWS.items():
        if view not in actual:
            problems.append(f"missing_view: {SVC_SCHEMA}.{view}")
            continue
        for column, expected_type in columns.items():
            found = actual[view].get(column)
            if found is None:
                problems.append(f"missing_column: {SVC_SCHEMA}.{view}.{column}")
            elif found != expected_type:
                problems.append(
                    f"wrong_column_type: {SVC_SCHEMA}.{view}.{column}"
                    f" is {found}, expected {expected_type}"
                )
    return problems
