"""HTTP Basic auth for the control-room console.

The rest of this repo authenticates on a single shared service token
(``imageshield.http.auth``) because there is no human in that loop — the
proxy is the only caller. The console is different: it has named human
operators, and the operator's name has to flow into every write it makes
(``decide``, ``create_event``, ``retract_event``) so the audit trail says who
acted, not "whoever holds the token". HTTP Basic against a small, explicit
roster (``CONSOLE_OPERATORS``) is the plain way to get a name out of a
request without building session infrastructure for an internal tool.

Every route except ``GET /health`` depends on :func:`require_operator`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac

from fastapi import Depends, Header, HTTPException, Request, status

from imageshield.console.config import ConsoleConfig

_WWW_AUTHENTICATE = 'Basic realm="imageshield-console"'


def parse_operators(raw: str) -> dict[str, str]:
    """Parse ``"name:token,name:token"`` into a name -> token map.

    Raises ``ValueError`` on an empty roster, a malformed entry, an empty
    name or token, or a duplicate name. Silently keeping the last of a
    duplicate would mean the roster's meaning depends on entry order, which
    is exactly the kind of ambiguity an audit trail cannot afford.
    """
    operators: dict[str, str] = {}
    entries = [entry for entry in raw.split(",") if entry.strip()]
    if not entries:
        raise ValueError("CONSOLE_OPERATORS must name at least one operator")
    for entry in entries:
        if ":" not in entry:
            raise ValueError(f"malformed operator entry {entry!r} (expected name:token)")
        name, _, token = entry.partition(":")
        name = name.strip()
        token = token.strip()
        if not name:
            raise ValueError(f"empty operator name in entry {entry!r}")
        if not token:
            raise ValueError(f"empty token for operator {name!r}")
        if name in operators:
            raise ValueError(f"duplicate operator name {name!r}")
        operators[name] = token
    return operators


def _parse_basic(header: str | None) -> tuple[str | None, str | None]:
    if header is None or not header.startswith("Basic "):
        return None, None
    encoded = header.removeprefix("Basic ")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None, None
    if ":" not in decoded:
        return None, None
    name, _, token = decoded.partition(":")
    return name, token


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid operator credentials",
        headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
    )


def get_console_config(request: Request) -> ConsoleConfig:
    config: ConsoleConfig = request.app.state.config
    return config


def require_operator(
    authorization: str | None = Header(default=None),
    cfg: ConsoleConfig = Depends(get_console_config),
) -> str:
    """Validate HTTP Basic credentials against ``CONSOLE_OPERATORS``.

    Constant-time per candidate; returns the operator NAME (never the
    token) so handlers can log and forward it without ever touching a
    secret. 401 + ``WWW-Authenticate: Basic`` on any failure -- missing
    header, malformed header, unknown name, or wrong token all look the
    same to the caller.
    """
    name, token = _parse_basic(authorization)
    operators = parse_operators(cfg.console_operators)
    expected = operators.get(name) if name is not None else None
    if name is None or token is None or expected is None:
        raise _unauthorized()
    if not _constant_time_equal(token, expected):
        raise _unauthorized()
    return name


def make_csrf_token(cfg: ConsoleConfig, operator: str) -> str:
    """A double-submit CSRF token for one operator's session.

    HMAC-SHA256 of the operator's name, keyed on ``admin_service_token`` --
    no new config, since that token is already a secret this process holds
    and never exposes. Binding the token to the operator name (rather than
    using one fixed value for everyone) means one operator's rendered form
    cannot be replayed under another operator's Basic credentials; binding it
    to a secret this console alone knows means an off-site page cannot
    compute a valid token to embed in a forged cross-site form post.
    """
    return hmac.new(
        cfg.admin_service_token.encode("utf-8"),
        operator.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_csrf_token(cfg: ConsoleConfig, operator: str, token: str) -> None:
    """Raise 403 on a missing or mismatched CSRF token.

    FastAPI's default ``HTTPException`` handler renders ``{"detail": ...}``
    as JSON, which is the "small JSON error" this is meant to produce --  no
    bespoke exception class needed for a single call site pattern used from
    three routes.
    """
    expected = make_csrf_token(cfg, operator)
    if not _constant_time_equal(token, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid or missing csrf token"
        )
