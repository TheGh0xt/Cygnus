"""Fail-closed request identity, via FastAPI's Depends — ROADMAP B.6.

Before this module, authentication was enforced *imperatively*, inside each
handler body: a handler called `current_user(request)` if and only if someone
remembered to write that line. A route that forgot — or was added later by
someone who didn't know the convention — defaulted to public. That is exactly
how `/v1/analyses/{id}` and its SSE stream ended up unauthenticated.

`get_current_user` inverts the default. It is attached to a router via
`APIRouter(dependencies=[Depends(get_current_user)])`, so it runs for every
route on that router whether or not the handler asks for its result. A route
is public only if it appears in `PUBLIC_ROUTES` below, by exact (method,
path-template) pair — there is no other way to opt out. A new route that
nobody allowlists is unreachable without a valid token; see
`test_new_route_defaults_to_401` in tests/api/test_auth_gate.py, which is the
guard that actually matters here.

`OPTIONAL_AUTH_ROUTES` is a second, narrower list for routes that implement
their *own* combined authorization — today, only `GET /v1/analyses/{id}`,
which accepts either the owner's token or a valid share token (see
sharing.py). Missing credentials there resolve to `None` instead of a 401,
and the handler decides; a *present but invalid* token still fails loudly,
because presenting a bad credential is different from presenting none.
"""

from __future__ import annotations

import logging

from fastapi import Request

from .auth import AuthError, CurrentUser, extract_bearer_token
from .errors import ErrorType, PmieError

logger = logging.getLogger("cygnus.api.access")

# Genuinely public: load balancers, uptime checks, and the onboarding screen
# reading the category list before anyone has signed in.
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/v1/health"),
        ("GET", "/v1/ready"),
        ("GET", "/v1/interests/categories"),
        # The reliability curve is an aggregate, not user data, and it is a
        # trust signal worth showing to a visitor who has not signed up yet.
        ("GET", "/v1/calibration"),
    }
)

# Routes with their own bespoke authorization, layered on top of (not instead
# of) this dependency. A missing token is not an error here; an invalid one
# still is.
OPTIONAL_AUTH_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/v1/analyses/{analysis_id}"),
    }
)


def _route_template(request: Request) -> str:
    """The path *template* FastAPI matched, e.g. "/v1/analyses/{analysis_id}".

    Matching on the template rather than the concrete path is what lets the
    allowlist stay a short, exact list instead of a set of regexes — and what
    stops `/v1/analyses/anything-i-type` from being mistaken for the public
    `/v1/health` route.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or request.url.path


def get_current_user(request: Request) -> CurrentUser | None:
    """The verified caller — or a 401 — for every route unless allowlisted.

    Local development can disable this entirely via PMIE_AUTH_DISABLED, read
    once at startup into app.state so a stray environment change cannot turn
    authentication off in a running process (see app.py).
    """
    if getattr(request.app.state, "auth_disabled", False):
        return CurrentUser(id="00000000-0000-0000-0000-000000000000", email=None)

    key = (request.method, _route_template(request))
    header = request.headers.get("authorization")

    if key in PUBLIC_ROUTES:
        return None

    if header is None:
        if key in OPTIONAL_AUTH_ROUTES:
            return None
        raise PmieError(ErrorType.INVALID_REQUEST, "Sign in to continue.", status=401)

    try:
        token = extract_bearer_token(header)
        return request.app.state.jwks.verify(token)
    except AuthError as exc:
        raise PmieError(
            ErrorType.INVALID_REQUEST, "Sign in to continue.", status=401
        ) from exc
    except Exception as exc:
        # A JWKS fetch failure is our problem, not the caller's, and must not
        # be reported as bad credentials.
        logger.exception("could not verify token")
        raise PmieError(
            ErrorType.INTERNAL_ERROR,
            "Could not verify your session. Try again shortly.",
            status=503,
        ) from exc


__all__ = [
    "OPTIONAL_AUTH_ROUTES",
    "PUBLIC_ROUTES",
    "get_current_user",
]
