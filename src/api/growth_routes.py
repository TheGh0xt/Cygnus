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
from .accounts import AccountsError
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

# `name` values are our own event vocabulary (e.g. "ui_mode_switched"), never
# free text, so lowercase snake_case is the actual contract, not just a nicety.
_EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_PROPERTIES = 20
_MAX_PROPERTY_KEY_LEN = 64
_MAX_PROPERTY_VALUE_LEN = 500


def _validate_event(body: UserEventRequest) -> None:
    """Bound what reaches `user_events.properties` (unbounded jsonb).

    Nothing else on the write path checks this — Growth.record_event forwards
    the dict verbatim — so an oversized or malformed payload would otherwise
    land straight in Supabase.
    """
    if not _EVENT_NAME_RE.match(body.name):
        raise PmieError(
            ErrorType.INVALID_REQUEST,
            "name must be lowercase snake_case, at most 64 characters.",
            status=422,
        )
    if body.name == "ui_mode_switched" and body.ui_mode is None:
        raise PmieError(
            ErrorType.INVALID_REQUEST,
            "ui_mode_switched requires ui_mode.",
            status=422,
        )
    if len(body.properties) > _MAX_PROPERTIES:
        raise PmieError(
            ErrorType.INVALID_REQUEST,
            f"properties may have at most {_MAX_PROPERTIES} keys.",
            status=422,
        )
    for key, value in body.properties.items():
        if not key or len(key) > _MAX_PROPERTY_KEY_LEN:
            raise PmieError(
                ErrorType.INVALID_REQUEST,
                f"property keys must be 1-{_MAX_PROPERTY_KEY_LEN} characters.",
                status=422,
            )
        if len(value) > _MAX_PROPERTY_VALUE_LEN:
            raise PmieError(
                ErrorType.INVALID_REQUEST,
                f"property values must be at most {_MAX_PROPERTY_VALUE_LEN} characters.",
                status=422,
            )


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
    _validate_event(body)

    growth = request.app.state.growth
    if not growth.configured:
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Event tracking is not configured.", status=503
        )

    # The profile PATCH (idempotent — it just sets ui_mode) runs before the
    # event log (not idempotent — each call appends a row), so a retry after
    # a failure here re-applies the same state instead of double-logging the
    # event. This is also the actual point of Profile.ui_mode/MeResponse.ui_mode
    # (Cygnus#32 review): a mode switch must persist on the profile, not just
    # get logged, so it survives a new device or session.
    if body.name == "ui_mode_switched" and body.ui_mode is not None:
        accounts = request.app.state.accounts
        if not accounts.configured:
            raise PmieError(
                ErrorType.INTERNAL_ERROR, "Accounts are not configured.", status=503
            )
        try:
            accounts.set_ui_mode(user.id, body.ui_mode.value)
        except AccountsError as exc:
            raise PmieError(
                ErrorType.INTERNAL_ERROR,
                "Could not update your preference.",
                status=503,
            ) from exc

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
