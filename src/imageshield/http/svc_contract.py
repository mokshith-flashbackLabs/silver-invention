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

**Three things are checked, not one.** The relations exist *as views*, their
columns are present and correctly typed, and `imageshield_proxy_ro` still holds
SELECT on all four. The grant is half the contract and it is the half whose
failure lands entirely on the other repo: revoke it and the views are still
present, still correctly shaped, and unreadable by the only role that reads
them. `relkind` matters for the same reason a name check is not enough —
`docs/DEPLOY-DEV-HANDOFF.md` §7 forbids `svc._stub_*` in a deployed environment,
and a stub *table* with the right columns satisfies every other assertion here
while serving a fixture's rows instead of this database's.

WHY `pg_catalog` AND NOT `information_schema`
---------------------------------------------
`information_schema` is **privilege-filtered** by the SQL standard: a role sees
only the columns it owns or holds some privilege on. Migration 0016 grants `svc`
USAGE/SELECT to `imageshield_proxy_ro` and to nobody else, and 0015 grants
`app_services` — the role this service actually connects as — nothing at all.
The views are owned by the migration runner.

Measured on a scratch database as a non-owner role with no grants on `svc`:
``information_schema.columns WHERE table_schema = 'svc'`` returns **0 rows**
while ``pg_views`` returns 4. Reading the information schema therefore made
/readyz answer 503 with four ``missing_view`` entries against a perfectly correct
database, and sent the operator to re-run a migration that had already succeeded.
`pg_catalog` is not privilege-filtered, so the probe now sees what is really
there rather than what its own connection role happens to own.

Types are ``format_type(atttypid, atttypmod)`` spellings — PostgreSQL's own
canonical form, which is not always ``information_schema.data_type``'s. Of the 33
columns here exactly one differs: ``v_person_hits.score`` reads as ``numeric``
there and ``numeric(6,4)`` here. The narrower spelling is the better contract,
because the precision is part of what the proxy deserialises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg import AsyncConnection

SVC_SCHEMA = "svc"

# The grant target migration 0016 creates. NOLOGIN by design — it is a grant
# target, not an identity — and 0017 grants membership in it to the proxy's login
# roles. Named here rather than inline so the readiness check and the migration
# cannot drift onto two different roles.
PROXY_ROLE = "imageshield_proxy_ro"

# pg_class.relkind for an ordinary view. A matview ('m') is deliberately NOT
# accepted: it would serve whatever was true at the last REFRESH, which for
# live_exposure_count means a user's report silently freezing.
_VIEW_RELKIND = "v"

# The relkinds that have columns at all. Restricting the sweep keeps indexes and
# TOAST relations out; a contract name existing as one of those reads as
# `missing_view`, which is the accurate report.
_RELATION_KINDS = ("r", "v", "m", "f", "p")

_RELKIND_NAMES = {
    "r": "table",
    "v": "view",
    "m": "materialized view",
    "f": "foreign table",
    "p": "partitioned table",
}

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
        # NUMERIC(6,4), inherited from attestations.provider_score. Spelled with
        # the precision because that is what format_type reports; the
        # information schema flattens it to "numeric".
        "score": "numeric(6,4)",
    },
    "v_person_liveness_attempts": {
        "person_ref": "uuid",
        "attempts_24h": "integer",
        "last_attempt_at": "timestamp with time zone",
    },
}

# LEFT JOIN to pg_attribute so a relation whose columns have all been dropped
# still appears — reported as missing columns rather than as a missing view,
# which is the more accurate diagnosis. attnum < 1 excludes system columns
# (ctid, tableoid, ...) and attisdropped excludes the tombstones a dropped
# column leaves behind.
_SHAPE_SQL = """
SELECT c.relname, c.relkind, a.attname, format_type(a.atttypid, a.atttypmod)
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_attribute a
       ON a.attrelid = c.oid AND a.attnum >= 1 AND NOT a.attisdropped
WHERE n.nspname = %s AND c.relkind = ANY(%s)
"""

_ROLE_EXISTS_SQL = "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s"

_SCHEMA_USAGE_SQL = "SELECT has_schema_privilege(%s, %s, 'USAGE')"

# has_table_privilege is called with the relation's **OID**, not its name. The
# name form resolves `svc.v_person_hits` through the search path, which requires
# USAGE on `svc` from the *caller* — and the caller here is `app_services`, which
# has none, so the name form raises `permission denied for schema svc` (measured).
# The OID form skips name resolution and answers for any role.
_GRANT_SQL = """
SELECT c.relname, has_table_privilege(%(role)s, c.oid, 'SELECT')
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %(schema)s
  AND c.relkind = ANY(%(kinds)s)
  AND c.relname = ANY(%(views)s)
"""


async def check_svc_contract(
    conn: AsyncConnection[Any], *, proxy_role: str = PROXY_ROLE
) -> list[str]:
    """Return one message per contract violation; empty list means healthy.

    Messages name the view and column because this endpoint is internal and on a
    private subnet — a readiness probe that says only "not ready" costs an hour
    of digging.

    ``proxy_role`` is a test seam for the role-absent branch. The default is the
    role migration 0016 creates, and it is the only value any deployment uses.
    """
    relkinds: dict[str, str] = {}
    columns: dict[str, dict[str, str]] = {}
    async with conn.cursor() as cur:
        await cur.execute(_SHAPE_SQL, (SVC_SCHEMA, list(_RELATION_KINDS)))
        for relname, relkind, attname, column_type in await cur.fetchall():
            relkinds[relname] = relkind
            if attname is not None:
                columns.setdefault(relname, {})[attname] = column_type

    problems: list[str] = []

    # The grant half of the contract. Order matters: has_table_privilege raises
    # on a role that does not exist, so existence is established first.
    async with conn.cursor() as cur:
        await cur.execute(_ROLE_EXISTS_SQL, (proxy_role,))
        role_exists = await cur.fetchone() is not None

    may_select: dict[str, bool] = {}
    if role_exists:
        async with conn.cursor() as cur:
            await cur.execute(_SCHEMA_USAGE_SQL, (proxy_role, SVC_SCHEMA))
            usage = await cur.fetchone()
            if usage is not None and not usage[0]:
                problems.append(
                    f"missing_schema_usage: {proxy_role} has no USAGE on"
                    f" {SVC_SCHEMA} — every view below is denied regardless of"
                    " its own SELECT grant"
                )
            await cur.execute(
                _GRANT_SQL,
                {
                    "role": proxy_role,
                    "schema": SVC_SCHEMA,
                    "kinds": list(_RELATION_KINDS),
                    "views": list(EXPECTED_VIEWS),
                },
            )
            may_select = {name: bool(granted) for name, granted in await cur.fetchall()}
    else:
        # Absence is reported, never treated as healthy. A missing grant target
        # means the contract reaches nobody, which is the failure 0017 exists to
        # close — and it presents to the proxy as "the svc views are missing",
        # i.e. the wrong place to look.
        problems.append(
            f"missing_grant_role: {proxy_role} does not exist — migration 0016"
            " creates it and 0017 grants the proxy's login roles membership;"
            " without it the four views below are readable by nobody"
        )

    for view, expected in EXPECTED_VIEWS.items():
        relkind = relkinds.get(view)
        if relkind is None:
            problems.append(f"missing_view: {SVC_SCHEMA}.{view}")
            continue
        if relkind != _VIEW_RELKIND:
            kind_name = _RELKIND_NAMES.get(relkind, f"relkind {relkind!r}")
            problems.append(
                f"not_a_view: {SVC_SCHEMA}.{view} is a {kind_name}, expected a"
                " view — a stub relation with the right columns serves a"
                " fixture's rows, not this database's"
            )
        actual = columns.get(view, {})
        for column, expected_type in expected.items():
            found = actual.get(column)
            if found is None:
                problems.append(f"missing_column: {SVC_SCHEMA}.{view}.{column}")
            elif found != expected_type:
                problems.append(
                    f"wrong_column_type: {SVC_SCHEMA}.{view}.{column}"
                    f" is {found}, expected {expected_type}"
                )
        if role_exists and not may_select.get(view, False):
            problems.append(
                f"missing_select_grant: {proxy_role} cannot SELECT"
                f" {SVC_SCHEMA}.{view}"
            )
    return problems
