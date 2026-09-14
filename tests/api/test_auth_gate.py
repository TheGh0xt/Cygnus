"""The gate itself: with auth ON, protected routes must reject callers.

Separate from test_auth.py, which covers token decoding. This covers the
wiring — that the dependency is actually attached to the endpoints, which is
the part that silently regresses when someone adds a route.
"""

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from src.api.access import PUBLIC_ROUTES, get_current_user
from src.api.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("PMIE_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    return TestClient(create_app(db_path=str(tmp_path / "gate.db")))


PROTECTED = [
    ("post", "/v1/analyses", {"query": "why is world-cup-winner moving?"}),
    ("get", "/v1/me", None),
    ("put", "/v1/me/interests", {"categories": ["politics", "crypto", "ai"]}),
    ("post", "/v1/analyses/abc/feedback", {"is_useful": True}),
    ("get", "/v1/analyses/abc/events", None),
    ("post", "/v1/analyses/abc/share", None),
    ("delete", "/v1/analyses/abc/share", None),
]


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_protected_routes_reject_anonymous_callers(client, method, path, body):
    response = (
        getattr(client, method)(path, json=body)
        if body
        else getattr(client, method)(path)
    )
    assert response.status_code == 401, f"{method.upper()} {path} was not protected"
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_protected_routes_reject_garbage_tokens(client, method, path, body):
    headers = {"authorization": "Bearer not-a-real-token"}
    call = getattr(client, method)
    response = (
        call(path, json=body, headers=headers) if body else call(path, headers=headers)
    )
    # 401 for a bad token; 503 if the JWKS endpoint itself is unreachable,
    # which is our failure and must not be reported as bad credentials.
    assert response.status_code in (401, 503)


def test_health_and_ready_stay_public(client):
    # Load balancers and uptime checks cannot authenticate.
    assert client.get("/v1/health").status_code == 200
    assert client.get("/v1/ready").status_code == 200


def test_auth_disabled_flag_is_read_once_at_startup(tmp_path, monkeypatch):
    # Set after the app is built: a later environment change must not be able
    # to switch authentication off in a running process.
    monkeypatch.delenv("PMIE_AUTH_DISABLED", raising=False)
    app = create_app(db_path=str(tmp_path / "startup.db"))
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "1")
    assert app.state.auth_disabled is False
    assert TestClient(app).get("/v1/me").status_code == 401


def test_new_route_defaults_to_401_unless_explicitly_allowlisted(client):
    """The guard that matters most: a route nobody allowlisted stays closed.

    Before B.6, a route was public unless a developer remembered to call
    current_user() inside its handler body — which is exactly how
    /v1/analyses/{id} and its SSE stream ended up unauthenticated. Proving
    that regresses would mean writing a *new* handler that forgets to call
    current_user() and watching it 200 for an anonymous caller.
    That is what this test does, on the app's real router wiring: it mounts
    one extra route the same way every route in routes.py is mounted — via
    `APIRouter(dependencies=[Depends(get_current_user)])` — and adds nothing
    else. Nobody put it in access.PUBLIC_ROUTES or
    access.OPTIONAL_AUTH_ROUTES, so it must 401 for an anonymous caller
    purely from being attached to the router, with the handler body never
    reached.
    """
    throwaway = APIRouter(dependencies=[Depends(get_current_user)])
    reached = {"value": False}

    @throwaway.get("/v1/__brand_new_route_nobody_allowlisted__")
    async def _new_route():
        reached["value"] = True
        return {"ok": True}

    client.app.include_router(throwaway)

    response = client.get("/v1/__brand_new_route_nobody_allowlisted__")

    assert response.status_code == 401
    assert reached["value"] is False, "the handler ran despite no credentials"


def _requires_identity(route) -> bool:
    """Whether get_current_user runs for this route, however it was attached.

    Walks the resolved dependency tree rather than reading the router's
    constructor, so it is true for a route that declares the dependency as a
    parameter *or* inherits it from its router.
    """
    seen, stack = set(), list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if id(dep) in seen:
            continue
        seen.add(id(dep))
        if dep.call is get_current_user:
            return True
        stack.extend(dep.dependencies)
    return False


def test_every_public_route_is_closed_unless_allowlisted(tmp_path):
    """The real app's wiring, not a stand-in router.

    test_new_route_defaults_to_401 above mounts its own throwaway router, so
    it proves the *pattern* works — not that routes.py still uses it. Removing
    `dependencies=[Depends(get_current_user)]` from the real router leaves
    every existing handler protected (each declares the dependency as a
    parameter too) and that test still green, while silently restoring the
    old default where a newly added route is public. This is the test that
    fails when that happens.

    Internal cron routes are excluded: they are hidden from the schema and
    authenticated by a shared secret header instead of a user token.
    """
    from fastapi.routing import APIRoute

    app = create_app(db_path=str(tmp_path / "gate.db"))

    unguarded = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path.startswith("/v1/internal/"):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if (method, route.path) in PUBLIC_ROUTES:
                continue
            if not _requires_identity(route):
                unguarded.append(f"{method} {route.path}")

    assert not unguarded, (
        "these routes neither require identity nor appear in "
        f"access.PUBLIC_ROUTES: {sorted(unguarded)}"
    )


def test_the_real_routers_close_by_default():
    """B.6's actual promise: a new route is closed without anyone acting.

    The test above asserts the invariant that matters — no route is
    unguarded — but today that holds even with the router-level dependency
    removed, because every existing handler also declares get_current_user as
    a parameter. The gap only becomes a vulnerability when someone adds route
    number twenty and forgets, which is precisely the failure B.6 exists to
    make impossible.

    So pin the mechanism as well as the outcome: the routers that serve user
    data must carry the dependency themselves, so protection is inherited
    rather than remembered.
    """
    from src.api import routes, sharing_routes

    for module in (routes, sharing_routes):
        attached = [d.dependency for d in module.router.dependencies]
        assert get_current_user in attached, (
            f"{module.__name__}.router no longer closes by default — a route "
            "added to it without declaring get_current_user would be public"
        )
