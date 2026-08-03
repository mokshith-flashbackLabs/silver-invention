"""Schema lint: enforcement for INVARIANTS.md #9 — no image bytes persisted, anywhere.

This module is pure and DB-free by design: it takes column metadata (whatever
shape a caller already has — ``information_schema.columns`` rows, a manual
fixture, a scratch DDL parse) and returns violations. The DB-backed test in
``tests/test_schema_lint.py`` is what actually reads ``information_schema`` and
feeds rows here; keeping the rule itself DB-free means it typechecks under
mypy strict and unit-tests without a live Postgres.

Priority order (CLAUDE.md §8 step 2, task-2 brief):
  (a) Type gate — the real rule. ``bytea`` (scalar or array) is a violation
      no matter what the column is named. This is what actually catches a
      smuggled ``thumbnail_blob`` or a renamed ``photo`` column.
  (b) Name gate. A handful of suffixes/substrings are banned regardless of
      declared type, because a TEXT column can hold base64-encoded bytes just
      as well as a bytea column can.
  (c) Allowlist. ``_uri``/`_url``/`_uris`/`_urls`` suffixes are pointers into
      the proxy's S3 and are explicitly fine — but only after (a) and (b) have
      had their say. A ``thumbnail_uri`` column must still fail: the reject
      regex wins over the allowlist. That asymmetry is deliberate and is the
      fixture that proves the gate isn't just a rubber stamp on any ``_uri``
      suffix.

Matching is on suffix/substring against the column *name*, never on the
substring "image" — ``reference_image_uri`` and ``audit_image_uris`` must
pass, and they do, because "image" is not itself banned.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# (a) bytea, scalar or array. Postgres reports array element type via
# udt_name with a leading underscore (e.g. "_bytea" for bytea[]).
_BYTEA_DATA_TYPES = {"bytea"}
_BYTEA_UDT_NAMES = {"bytea", "_bytea"}

# (b) name gate: suffix match for the "encoded bytes as text" shapes, plus
# substring match for the two words that mean "we kept pixels around".
_REJECT_SUFFIX_RE = re.compile(r"_(data|blob|bytes|b64)$", re.IGNORECASE)
_REJECT_SUBSTRING_RE = re.compile(r"thumbnail|local_path", re.IGNORECASE)

# (c) allowlist: pointer-into-S3 suffixes. Checked only after (a)/(b) clear.
_ALLOW_SUFFIX_RE = re.compile(r"_(uri|url|uris|urls)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """The subset of ``information_schema.columns`` the lint needs.

    ``table_schema``/``table_name`` are carried through purely so violations
    can be reported with a useful location; they play no part in the rule.
    """

    table_schema: str
    table_name: str
    column_name: str
    data_type: str
    udt_name: str


@dataclass(frozen=True, slots=True)
class Violation:
    table_schema: str
    table_name: str
    column_name: str
    data_type: str
    udt_name: str
    reason: str

    def __str__(self) -> str:
        return (
            f"{self.table_schema}.{self.table_name}.{self.column_name} "
            f"({self.data_type}/{self.udt_name}): {self.reason}"
        )


def _type_violation_reason(column: ColumnInfo) -> str | None:
    if column.data_type.lower() in _BYTEA_DATA_TYPES:
        return f"column type is {column.data_type!r} (raw bytes)"
    if column.udt_name.lower() in _BYTEA_UDT_NAMES:
        return f"column udt_name is {column.udt_name!r} (raw bytes, possibly an array)"
    return None


def _name_violation_reason(column: ColumnInfo) -> str | None:
    name = column.column_name
    if _REJECT_SUFFIX_RE.search(name):
        return "column name matches banned suffix _(data|blob|bytes|b64)$"
    if _REJECT_SUBSTRING_RE.search(name):
        return "column name contains a banned substring (thumbnail/local_path)"
    return None


def _is_allowlisted(column: ColumnInfo) -> bool:
    return bool(_ALLOW_SUFFIX_RE.search(column.column_name))


def lint_column(column: ColumnInfo) -> Violation | None:
    """Apply the three-step gate to a single column. ``None`` means it passes."""
    reason = _type_violation_reason(column)
    if reason is not None:
        return Violation(
            table_schema=column.table_schema,
            table_name=column.table_name,
            column_name=column.column_name,
            data_type=column.data_type,
            udt_name=column.udt_name,
            reason=reason,
        )

    reason = _name_violation_reason(column)
    if reason is not None:
        return Violation(
            table_schema=column.table_schema,
            table_name=column.table_name,
            column_name=column.column_name,
            data_type=column.data_type,
            udt_name=column.udt_name,
            reason=reason,
        )

    if _is_allowlisted(column):
        return None

    # Not bytea, not banned by name, not explicitly allowlisted either: this
    # column is simply outside the rule's concern (e.g. an INT or a plain TEXT
    # status column) and passes by default. The allowlist exists to document
    # the pointer-into-S3 shape, not to gate everything else.
    return None


def lint_columns(rows: Iterable[ColumnInfo]) -> list[Violation]:
    """Lint every column, in order, returning all violations found."""
    violations: list[Violation] = []
    for column in rows:
        violation = lint_column(column)
        if violation is not None:
            violations.append(violation)
    return violations
