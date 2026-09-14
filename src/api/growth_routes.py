"""Growth endpoints: pre-signup capture (B.15) and product telemetry (B.19).

Two routers, deliberately. `router` carries no `get_current_user` dependency
— a visitor reaching the landing page has no account yet — so `/waitlist`
must instead be listed in `access.PUBLIC_ROUTES` explicitly, or
`test_every_public_route_is_closed_unless_allowlisted` fails; see access.py's
docstring for why that's the only way to opt out. `authenticated_router`
carries the dependency the ordinary way (routes.py/sharing_routes.py's
pattern): `/events` records product telemetry, which only makes sense
attributed to a signed-in caller.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request

from .access import get_current_user
from .auth import CurrentUser
from .errors import ErrorType, PmieError
from .growth import GrowthError
from .models import ProblemResponse, UserEventRequest, WaitlistRequest, WaitlistResponse

PROBLEM = {
    "model": ProblemResponse,
    "content": {"application/problem+json": {}},
}

router = APIRouter(prefix="/v1")
authenticated_router = APIRouter(prefix="/v1", dependencies=[Depends(get_current_user)])

# Deliberately permissive — this rejects only what has no chance of being a
# deliverable address, not what an RFC 5322 parser would. A landing-page form
# should not reject a real inbox over pedantry.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.post(
    "/waitlist",
    response_model=WaitlistResponse,
    summary="Join the beta waitlist (public)",
    responses={
        422: {"description": "Malformed email address", **PROBLEM},
        503: {"description": "The waitlist store is unavailable", **PROBLEM},
    },
)
# Plain `def`, not `async def`: a docstring here would leak into the public
# OpenAPI description, so the reasoning lives here instead. Growth.join_waitlist
# makes a blocking httpx call; FastAPI runs a sync endpoint in the threadpool,
# while an `async def` wrapper around blocking I/O would run it inline on the
# single event loop this process uses. One slow Supabase response would then
# stall every other request — including every SSE analysis stream in progress
# — and this is the one endpoint on the whole API that requires no token.
def join_waitlist(body: WaitlistRequest, request: Request) -> WaitlistResponse:
    if not _EMAIL_RE.match(body.email):
        raise PmieError(
            ErrorType.INVALID_REQUEST, "Enter a valid email address.", status=422
        )

    growth = request.app.state.growth
    if not growth.configured:
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "The waitlist is not configured.", status=503
        )

    try:
        result = growth.join_waitlist(body.email, body.referral_code)
    except GrowthError as exc:
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Could not join the waitlist.", status=503
        ) from exc

    return WaitlistResponse(position=None, already_registered=result.already_registered)


@authenticated_router.post(
    "/events",
    status_code=204,
    summary="Record a product event (UI mode switch, analysis started)",
    responses={
        401: {
            "description": "Not signed in, or the token failed verification",
            **PROBLEM,
        },
        503: {"description": "A dependency is unavailable", **PROBLEM},
    },
)
# Plain `def`: record_event makes only a blocking Supabase call, so this
# follows join_waitlist's fix above rather than needing async + threadpool
# offload — see that function's comment for the full reasoning.
def record_event(
    body: UserEventRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> None:
    growth = request.app.state.growth
    if not growth.configured:
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Event tracking is not configured.", status=503
        )

    try:
        growth.record_event(
            user.id,
            body.name,
            body.ui_mode.value if body.ui_mode else None,
            body.properties,
        )
    except GrowthError as exc:
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Could not record that event.", status=503
        ) from exc
