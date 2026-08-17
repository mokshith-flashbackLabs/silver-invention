"""Compose a ``postgres://`` connection URL from discrete parts.

The dev Secrets Manager secret (``imageshield/dev/db/app_services``) is
RDS-shaped: exactly ``dbname``, ``host``, ``password``, ``port``, ``username``
— no ``url`` key. An ECS ``secrets`` entry injects exactly one JSON key per
environment variable, so there is no key on that secret that can produce a
single ``DATABASE_URL`` directly (``docs/DEPLOY-DEV-HANDOFF.md`` §4-§5).

This module is the one place that turns those parts into a URL string, so the
percent-encoding rule cannot drift between the two callers:
:mod:`imageshield.config` (composing from its own already-parsed ``db_*``
fields, for the HTTP service and the relay) and ``scripts/migrate.py``
(composing from raw ``os.environ`` values directly, since that script
deliberately has no dependency on ``Config`` — it needs exactly one value,
and ``Config`` requires dozens of unrelated ones).
"""

from __future__ import annotations

from urllib.parse import quote


def compose_database_url(
    *,
    host: str,
    port: int | str,
    name: str,
    user: str,
    password: str,
    sslmode: str = "require",
) -> str:
    """Build a ``postgresql://`` URL, percent-encoding ``user``/``password``.

    A generated RDS password may contain ``@ / : # ? %``; unencoded, any of
    those either breaks the URL or — worse — parses as a different host.
    ``quote(..., safe="")`` encodes everything the URL userinfo grammar does
    not allow literally, including a literal ``%`` itself (so a password that
    already contains one round-trips instead of being misread as the start of
    another escape). ``host``/``name`` are not encoded: neither is stated to
    require it, and both are simple identifiers (an RDS hostname, a database
    name) in every environment this composes for.

    ``sslmode`` defaults to ``require`` and that default is not cosmetic: the
    RDS parameter group sets ``rds.force_ssl = 1``, so a connection without it
    is refused. Callers must never pass anything weaker as a default.
    """
    quoted_user = quote(user, safe="")
    quoted_password = quote(password, safe="")
    return f"postgresql://{quoted_user}:{quoted_password}@{host}:{port}/{name}?sslmode={sslmode}"
