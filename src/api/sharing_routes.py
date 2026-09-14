"""Minting and revoking a report's public link — ROADMAP B.6.

Retired from contract_stubs.py: the shape was frozen there, the logic lives
here. Both routes are owner-only — there is no PUBLIC_ROUTES or
OPTIONAL_AUTH_ROUTES entry for them in access.py, so a missing or invalid
token 401s before either handler runs.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from .access import get_current_user
from .auth import CurrentUser
from .errors import ErrorType, PmieError
from .models import ShareTokenResponse
from .sharing import SharingError

logger = logging.getLogger("cygnus.api.sharing")

router = APIRouter(prefix="/v1", dependencies=[Depends(get_current_user)])


def _owned_record(request: Request, analysis_id: str, user: CurrentUser):
    record = request.app.state.registry.get(analysis_id)
    if record is None:
        raise PmieError(
            ErrorType.ANALYSIS_NOT_FOUND, f"no analysis {analysis_id}", status=404
        )
    if record.profile_id != user.id:
        raise PmieError(
            ErrorType.INVALID_REQUEST,
            "This analysis belongs to another account.",
            status=403,
        )
    if record.report_id is None:
        raise PmieError(
            ErrorType.INVALID_REQUEST,
            "This analysis has not finished, or was not persisted, so there "
            "is nothing to share yet.",
            status=409,
        )
    return record


@router.post(
    "/analyses/{analysis_id}/share",
    response_model=ShareTokenResponse,
    summary="Mint a public read-only link for a report",
)
async def create_share_token(
    analysis_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> ShareTokenResponse:
    record = _owned_record(request, analysis_id, user)
    sharing = request.app.state.sharing
    if not sharing.configured:
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Sharing is not configured.", status=503
        )
    try:
        share = sharing.create(record.report_id, created_by=user.id)
    except SharingError as exc:
        logger.exception("could not create share token")
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Could not create a share link.", status=503
        ) from exc

    base = str(request.base_url).rstrip("/")
    return ShareTokenResponse(
        token=share.token,
        url=f"{base}/v1/analyses/{analysis_id}?share_token={share.token}",
        expires_at=share.expires_at,
    )


@router.delete(
    "/analyses/{analysis_id}/share",
    status_code=204,
    summary="Revoke a report's public link",
)
async def revoke_share_token(
    analysis_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> None:
    record = _owned_record(request, analysis_id, user)
    sharing = request.app.state.sharing
    if not sharing.configured:
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Sharing is not configured.", status=503
        )
    try:
        sharing.revoke(record.report_id, revoked_by=user.id)
    except SharingError as exc:
        logger.exception("could not revoke share token")
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Could not revoke the share link.", status=503
        ) from exc
