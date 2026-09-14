"""GET /v1/markets/moving — ROADMAP B.17.

Its own router, fail-closed the same way routes.py and sharing_routes.py are:
`get_current_user` is attached at the router level, not remembered per
handler, because this is exactly the kind of route B.6 exists to protect —
it reads back one signed-in user's chosen categories.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool

from ..generation.discovery import DiscoveryError
from .access import get_current_user
from .accounts import AccountsError
from .auth import CurrentUser
from .discovery import to_moving_market
from .errors import ErrorType, PmieError
from .models import MovingMarketsResponse, ProblemResponse

logger = logging.getLogger("cygnus.api.discovery")

PROBLEM = {
    "model": ProblemResponse,
    "content": {"application/problem+json": {}},
}

router = APIRouter(prefix="/v1", dependencies=[Depends(get_current_user)])

# UI_PRD 6.4 asks for "markets moving now", not an exhaustive list — this
# caps the feed the same way SagittariusDiscovery.moving_markets defaults,
# named here so the route's own limit is visible without following the call.
_FEED_LIMIT = 20


@router.get(
    "/markets/moving",
    response_model=MovingMarketsResponse,
    summary="Markets moving now, in the user's categories",
    responses={
        401: {
            "description": "Not signed in, or the token failed verification",
            **PROBLEM,
        },
        503: {"description": "A dependency is unavailable", **PROBLEM},
    },
)
async def moving_markets(
    request: Request, user: CurrentUser = Depends(get_current_user)
) -> MovingMarketsResponse:
    accounts = request.app.state.accounts
    categories: list[str] = []
    if accounts.configured:
        try:
            # accounts.get_interests makes a blocking httpx call; off the
            # event loop so it can't stall every other request (including
            # in-progress SSE analysis streams) the way join_waitlist did
            # before Cygnus#26. This handler can't be a plain `def` instead,
            # the way that fix was: the Sagittarius call below is genuinely
            # async and has to be awaited.
            categories = await run_in_threadpool(accounts.get_interests, user.id)
        except AccountsError as exc:
            raise PmieError(
                ErrorType.INTERNAL_ERROR, "Could not load your categories.", status=503
            ) from exc

    discovery = request.app.state.discovery
    try:
        # None, not []: a user with no chosen categories yet (pre-onboarding)
        # still gets a feed, unfiltered, rather than a wall of empty state.
        candidates = await discovery.moving_markets(
            categories=categories or None, limit=_FEED_LIMIT
        )
    except DiscoveryError as exc:
        logger.error("could not load moving markets: %s", exc)
        raise PmieError(
            ErrorType.SAGITTARIUS_UNAVAILABLE,
            "Could not load moving markets.",
            status=503,
        ) from exc

    return MovingMarketsResponse(
        markets=[to_moving_market(candidate) for candidate in candidates],
        categories=categories,
    )
