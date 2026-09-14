"""Public growth endpoints — ROADMAP B.15.

Unlike routes.py and sharing_routes.py, this router carries no
`get_current_user` dependency: a visitor reaching the landing page has no
account yet. Each route here must instead be listed in `access.PUBLIC_ROUTES`
explicitly, or `test_every_public_route_is_closed_unless_allowlisted` fails —
see access.py's docstring for why that's the only way to opt out.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request

from .errors import ErrorType, PmieError
from .growth import GrowthError
from .models import ProblemResponse, WaitlistRequest, WaitlistResponse

PROBLEM = {
    "model": ProblemResponse,
    "content": {"application/problem+json": {}},
}

router = APIRouter(prefix="/v1")

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
