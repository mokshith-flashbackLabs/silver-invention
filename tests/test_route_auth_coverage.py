"""Route-auth coverage, parameterised over ``app.routes``.

Step 9's blocking gate: **a new route without the service-token dependency
fails the build.** Enumerating the app's own route table rather than listing
paths by hand is the whole point — a hand-written list is a list somebody
forgets to add to, and the route it omits is the one that ships unauthenticated.

Behavioural rather than structural: each route is actually called without a
token and must answer 401. Inspecting ``route.dependant`` would prove a
dependency is *attached*; calling proves it *fires*, which is the property that
matters and the one a refactor can silently break.

This service has no per-user auth and no public ingress (CLAUDE.md §3.1); the
service token is the entire authentication story. A route that skips it is
reachable by anything that reaches the private subnet.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute, APIRouter
from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config

# Deliberately tiny, and every entry needs a reason.
#
# /health is the load balancer's probe: it must answer before anything is
# configured, and it deliberately returns no data (tests/test_auth.py asserts
# both). Everything else in the app is behind a token.
_UNAUTHENTICATED_PATHS = frozenset({"/health"})

_PATH_PARAM_RE = re.compile(r"\{[^}]+\}")


def _walk(routes: list[Any]) -> list[APIRoute]:
    """Every APIRoute, recursively.

    FastAPI 0.141 keeps included routers NESTED — ``app.routes`` holds
    ``_IncludedRouter`` wrappers, not the flattened table older versions
    produced. A gate that only looked at the top level would enumerate ZERO
    routes and pass forever, which is why ``test_the_route_table_is_not_empty``
    exists below.
    """
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
            continue
        inner = getattr(route, "original_router", None)
        if isinstance(inner, APIRouter):
            found.extend(_walk(inner.routes))
    return found


def _routes() -> list[APIRoute]:
    return _walk(create_app(config=make_config()).routes)


def _concrete(path: str) -> str:
    """A callable URL: path params filled with a UUID.

    The value never matters — auth is resolved before the handler runs, so a
    route that is properly guarded answers 401 without ever looking at it.
    """
    return _PATH_PARAM_RE.sub(str(uuid4()), path)


def _one_method(route: APIRoute) -> str:
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        if method in route.methods:
            return method
    raise AssertionError(f"{route.path} declares no usable method")


def _call(client: TestClient, route: APIRoute, headers: dict[str, str]) -> Any:
    return client.request(
        _one_method(route),
        _concrete(route.path),
        json={},
        headers=headers,
    )


_ROUTES = _routes()
_GUARDED = [r for r in _ROUTES if r.path not in _UNAUTHENTICATED_PATHS]
_ADMIN = [r for r in _GUARDED if r.path.startswith("/v1/admin")]


def _ident(route: APIRoute) -> str:
    return f"{_one_method(route)} {route.path}"


def test_the_route_table_is_not_empty() -> None:
    """A gate that silently enumerates nothing passes forever. This is the
    tripwire on the tripwire."""
    assert len(_GUARDED) >= 10, f"only found {len(_GUARDED)} routes — introspection broken?"


@pytest.mark.parametrize("route", _GUARDED, ids=_ident)
def test_every_route_refuses_a_request_with_no_token(route: APIRoute) -> None:
    client = TestClient(create_app(config=make_config()))

    response = _call(client, route, {})

    assert response.status_code == 401, (
        f"{_ident(route)} answered {response.status_code} with NO service token."
        " Every route on this service must carry require_service_token —"
        " there is no per-user auth and no public ingress, so the token is the"
        " entire authentication story."
    )


@pytest.mark.parametrize("route", _ADMIN, ids=_ident)
def test_every_admin_route_also_refuses_a_plain_service_token(route: APIRoute) -> None:
    """The two tokens must differ (boot refuses if they match), and an admin
    route reachable with the ordinary token would make that separation
    decorative. Kill switches and breaker resets live behind these."""
    client = TestClient(create_app(config=make_config()))

    response = _call(client, route, {"X-Service-Token": SERVICE_TOKEN})

    assert response.status_code == 401, (
        f"{_ident(route)} accepted the plain service token. Admin routes need"
        " X-Admin-Service-Token as well."
    )


def test_there_is_exactly_one_admin_prefix() -> None:
    """Step 8 collapsed /admin/* into /v1/admin/*. A gate that walks one of two
    admin surfaces is worse than no gate, because it reads as coverage."""
    admin_like = [
        r.path for r in _ROUTES if "admin" in r.path and not r.path.startswith("/v1/admin/")
    ]
    assert admin_like == []


def test_admin_routes_are_reachable_with_both_tokens() -> None:
    """The negative tests above would all pass if every admin route were simply
    broken. One positive case proves the 401s mean 'wrong credentials' rather
    than 'nothing here'."""
    assert _ADMIN, "no admin routes found — introspection broken?"
    client = TestClient(create_app(config=make_config()))
    route = next(r for r in _ADMIN if "GET" in r.methods)

    response = _call(
        client,
        route,
        {
            "X-Service-Token": SERVICE_TOKEN,
            "X-Admin-Service-Token": ADMIN_SERVICE_TOKEN,
        },
    )

    assert response.status_code != 401
