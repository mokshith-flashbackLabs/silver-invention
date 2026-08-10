"""Service-token auth dependencies.

Adapted from the Flashback agent service (AgentMeeMaw src/flashback/http/auth.py).
The proxy is the real auth boundary (CLAUDE.md §3); this is defence-in-depth so
we don't blindly trust the network. Comparison via :func:`hmac.compare_digest`,
never ``==``.

Divergences from Flashback, both deliberate:
- ``ADMIN_SERVICE_TOKEN`` is always required and always distinct from
  ``SERVICE_TOKEN`` (enforced in config) — no fallback when auth is disabled.
- The ``SERVICE_TOKEN_AUTH_DISABLED=1`` bypass refuses to take effect unless
  ``ENVIRONMENT == 'development'`` (gated in ``Config.auth_disabled``).

Apply ``require_service_token`` as a router-level dependency on every router
except ``/health``; ``/v1/admin/*`` routers additionally take
``require_admin_service_token``. There is exactly one admin prefix — see
``routes/ping.py`` for why that matters to step 9's route-auth gate.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, status

from imageshield.config import Config
from imageshield.http.deps import get_config


def require_service_token(
    x_service_token: str | None = Header(default=None, alias="X-Service-Token"),
    cfg: Config = Depends(get_config),
) -> None:
    """Validate ``X-Service-Token``. Purely a guard — handlers never see the token."""
    if cfg.auth_disabled:
        return
    if x_service_token is None or not _constant_time_equal(x_service_token, cfg.service_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid service token",
        )


def require_admin_service_token(
    x_admin_service_token: str | None = Header(default=None, alias="X-Admin-Service-Token"),
    cfg: Config = Depends(get_config),
) -> None:
    """Validate ``X-Admin-Service-Token`` — required *in addition* to the service token."""
    if cfg.auth_disabled:
        return
    if x_admin_service_token is None or not _constant_time_equal(
        x_admin_service_token, cfg.admin_service_token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid admin service token",
        )


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
