"""Deliberately ill-typed. Excluded from the repo-wide mypy run; the
test_typecheck gate runs mypy --strict on this file and asserts it FAILS —
proving a bare str (or bare UUID) cannot pass where UserRef is expected."""

from uuid import UUID

from imageshield.types import SessionId, UserRef


def needs_user_ref(ref: UserRef) -> None:
    pass


x: UserRef = "0b6ad478-9c2f-4f0e-9c39-000000000000"  # bare str -> error
y: UserRef = UUID("0b6ad478-9c2f-4f0e-9c39-000000000000")  # bare UUID -> error
needs_user_ref("0b6ad478-9c2f-4f0e-9c39-000000000000")  # bare str arg -> error
z: UserRef = SessionId(UUID("0b6ad478-9c2f-4f0e-9c39-000000000000"))  # wrong id kind -> error
