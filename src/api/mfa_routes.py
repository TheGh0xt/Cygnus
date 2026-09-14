"""POST /me/mfa/enroll, POST /me/mfa/verify, GET /me/mfa — ROADMAP B.7.

Own router, fail-closed the same way routes.py/sharing_routes.py/
discovery_routes.py are — these read and mutate one signed-in user's own MFA
factors, exactly what B.6 exists to protect.

Every handler here is a plain `def`, not `async def`: every call this module
makes (Supabase Auth over httpx) is blocking, with nothing genuinely async to
await — unlike discovery_routes.py, which awaits a real MCP client. FastAPI
runs a sync endpoint in the threadpool automatically, so there is nothing to
wrap by hand.

`recovery_codes` in the enroll response is always `[]` — see mfa.py's
docstring for why that part of B.7 isn't built.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request

from .access import get_current_user
from .auth import extract_bearer_token
from .errors import ErrorType, PmieError
from .mfa import InvalidCode, MfaError
from .models import (
    MfaEnrollResponse,
    MfaStatusResponse,
    MfaVerifyRequest,
    ProblemResponse,
)

logger = logging.getLogger("cygnus.api.mfa")

PROBLEM = {
    "model": ProblemResponse,
    "content": {"application/problem+json": {}},
}
_AUTH_ERRORS = {
    401: {"description": "Not signed in, or the token failed verification", **PROBLEM},
    503: {"description": "A dependency is unavailable", **PROBLEM},
}

router = APIRouter(prefix="/v1/me", dependencies=[Depends(get_current_user)])


def _user_token(request: Request) -> str:
    """The caller's own access token, forwarded to Supabase Auth.

    get_current_user has already verified this header by the time a handler
    runs (it's the router's own dependency), so re-extracting it here just
    recovers the raw string — it does not re-check anything.
    """
    return extract_bearer_token(request.headers.get("authorization"))


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@router.post(
    "/mfa/enroll",
    response_model=MfaEnrollResponse,
    summary="Begin TOTP enrolment",
    responses=_AUTH_ERRORS,
)
def enroll_mfa(request: Request) -> MfaEnrollResponse:
    mfa = request.app.state.mfa
    if not mfa.configured:
        raise PmieError(ErrorType.INTERNAL_ERROR, "MFA is not configured.", status=503)

    token = _user_token(request)
    try:
        enrolled = mfa.enroll(token)
    except MfaError as exc:
        logger.error("TOTP enrolment failed: %s", exc)
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Could not begin TOTP enrolment.", status=503
        ) from exc

    return MfaEnrollResponse(
        factor_id=enrolled.factor_id,
        secret=enrolled.secret,
        qr_uri=enrolled.qr_uri,
        # See this module's docstring: recovery codes are a pending scope
        # decision, not something to fabricate.
        recovery_codes=[],
    )


@router.post(
    "/mfa/verify",
    response_model=MfaStatusResponse,
    summary="Confirm a TOTP code and activate the factor",
    responses={
        **_AUTH_ERRORS,
        422: {"description": "The code is incorrect or has expired", **PROBLEM},
    },
)
def verify_mfa(body: MfaVerifyRequest, request: Request) -> MfaStatusResponse:
    mfa = request.app.state.mfa
    if not mfa.configured:
        raise PmieError(ErrorType.INTERNAL_ERROR, "MFA is not configured.", status=503)

    token = _user_token(request)
    try:
        mfa.verify(token, body.factor_id, body.code)
    except InvalidCode as exc:
        raise PmieError(
            ErrorType.INVALID_REQUEST,
            "That code is incorrect or has expired.",
            status=422,
        ) from exc
    except MfaError as exc:
        logger.error("TOTP verification failed: %s", exc)
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Could not verify that code.", status=503
        ) from exc

    return MfaStatusResponse(enrolled=True, verified_at=datetime.now(UTC))


@router.get(
    "/mfa",
    response_model=MfaStatusResponse,
    summary="Whether TOTP is active for this user",
    responses=_AUTH_ERRORS,
)
def mfa_status(request: Request) -> MfaStatusResponse:
    mfa = request.app.state.mfa
    if not mfa.configured:
        raise PmieError(ErrorType.INTERNAL_ERROR, "MFA is not configured.", status=503)

    token = _user_token(request)
    try:
        factor_status = mfa.status(token)
    except MfaError as exc:
        logger.error("could not load MFA status: %s", exc)
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Could not load your MFA status.", status=503
        ) from exc

    return MfaStatusResponse(
        enrolled=factor_status.enrolled,
        verified_at=_parse_ts(factor_status.verified_at),
    )
