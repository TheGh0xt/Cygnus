"""The frozen contract — routes whose shape is agreed before their logic exists.

Wave 0 of the beta push freezes every cross-repo contract before any parallel
work starts, because the alternative is three repos negotiating interfaces with
each other mid-build. Lyra generates its typed client from `openapi.json`,
which is generated from this app, so a route absent here cannot be built
against — and a UI team blocked on a backend team is exactly the serialisation
the freeze exists to avoid.

Every route below returns RFC 9457 `not-implemented` with 501. That is a
deliberate, visible placeholder, not a silent one: a client that calls it gets
a machine-readable answer saying so, and the endpoint appears in the schema
with its real request and response models.

**How to retire a stub.** Move the route into the module that owns its concern,
implement it, and delete it from here. This file should shrink to nothing. If
it stops shrinking, the freeze has become a backlog.

Owners, by ROADMAP §0b task:
    B.7   TOTP enrolment            → auth/MFA
    B.10  referrals, quota, intent  → accounts
    B.11  explanation calibration   → evaluation
    B.19  UI-mode telemetry         → growth

Already retired: B.15 (waitlist) — see growth.py / growth_routes.py.
B.17 (moving-markets feed) — see discovery.py / discovery_routes.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .access import get_current_user
from .errors import ErrorType, PmieError
from .models import (
    CalibrationResponse,
    MfaEnrollResponse,
    MfaStatusResponse,
    MfaVerifyRequest,
    PayIntentRequest,
    ReferralSummary,
    UserEventRequest,
)

# Same fail-closed default as routes.py and sharing_routes.py: every stub
# below will be a real, identity-scoped endpoint once it's built (mfa,
# referrals, billing intent and events are all per-user; see each task's
# acceptance criteria), so it inherits get_current_user now rather than
# gaining it as an afterthought when the 501 is replaced with real logic.
# The two genuinely public stubs (waitlist — since retired — and
# calibration) opt out via access.PUBLIC_ROUTES, the only way to do so.
router = APIRouter(prefix="/v1", dependencies=[Depends(get_current_user)])

_RESPONSES: dict[int | str, dict] = {
    501: {
        "description": "Not implemented yet — the contract is frozen, the logic is not."
    }
}


def _stub(task: str) -> None:
    """Fail loudly and machine-readably, naming the task that will land it."""
    raise PmieError(
        ErrorType.NOT_IMPLEMENTED,
        f"This endpoint's contract is frozen but its logic is not built yet ({task}).",
        501,
    )


# ── B.7 — TOTP, via Supabase MFA factors ─────────────────────────────────────


@router.post(
    "/me/mfa/enroll",
    response_model=MfaEnrollResponse,
    responses=_RESPONSES,
    summary="Begin TOTP enrolment",
)
async def enroll_mfa() -> MfaEnrollResponse:
    _stub("B.7")


@router.post(
    "/me/mfa/verify",
    response_model=MfaStatusResponse,
    responses=_RESPONSES,
    summary="Confirm a TOTP code and activate the factor",
)
async def verify_mfa(_body: MfaVerifyRequest) -> MfaStatusResponse:
    _stub("B.7")


@router.get(
    "/me/mfa",
    response_model=MfaStatusResponse,
    responses=_RESPONSES,
    summary="Whether TOTP is active for this user",
)
async def mfa_status() -> MfaStatusResponse:
    _stub("B.7")


# ── B.10 — referrals and the intent wall ─────────────────────────────────────


@router.get(
    "/me/referrals",
    response_model=ReferralSummary,
    responses=_RESPONSES,
    summary="This user's referral code and its standing",
)
async def referrals() -> ReferralSummary:
    _stub("B.10")


@router.post(
    "/billing/intent",
    status_code=204,
    responses=_RESPONSES,
    summary="Record willingness to pay at the price shown",
)
async def record_pay_intent(_body: PayIntentRequest) -> None:
    # No money moves during beta. This records that someone who had already
    # used the product wanted more of it at a stated price.
    _stub("B.10")


# ── B.19 — UI-mode preference telemetry ──────────────────────────────────────


@router.post(
    "/events",
    status_code=204,
    responses=_RESPONSES,
    summary="Record a product event (UI mode switch, analysis started)",
)
async def record_event(_body: UserEventRequest) -> None:
    _stub("B.19")


# ── B.11 — explanation calibration ───────────────────────────────────────────


@router.get(
    "/calibration",
    response_model=CalibrationResponse,
    responses=_RESPONSES,
    summary="Reliability of PMIE's own confidence scores",
)
async def calibration() -> CalibrationResponse:
    # Explanation calibration, not a forecast record: "when PMIE says 0.7, is
    # it right about 70% of the time?" It scores no prediction of a market
    # outcome, because PMIE makes none.
    _stub("B.11")
