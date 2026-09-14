"""GET /v1/calibration — ROADMAP B.11.

Public, like growth_routes.py: a visitor has no account yet, and the
reliability curve is an aggregate trust signal, not user data — see
access.PUBLIC_ROUTES. This router carries no get_current_user dependency;
opting out any other way fails
test_every_public_route_is_closed_unless_allowlisted.

This answers "when PMIE says 0.7, is it right about 70% of the time?" — never
a forecast of market outcomes, which PMIE does not make. See
src/evaluation/calibration.py for the binning itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request

from ..evaluation.calibration import compute_calibration
from .models import CalibrationBin, CalibrationResponse

router = APIRouter(prefix="/v1")


@router.get(
    "/calibration",
    response_model=CalibrationResponse,
    summary="Reliability of PMIE's own confidence scores",
)
async def calibration(request: Request) -> CalibrationResponse:
    store = request.app.state.memory_store

    # No store configured (e.g. a from-scratch dev environment) reads the
    # same as "nothing scored yet" — the insufficient-data state, not an
    # error. A trust-signal endpoint that 500s is worse than one that says
    # "still collecting".
    scored = store.get_scored_reports() if store is not None else []
    pairs = [(r.stated_confidence, r.outcome) for r in scored]

    result = compute_calibration(pairs)

    return CalibrationResponse(
        bins=[
            CalibrationBin(
                lower=b.lower,
                upper=b.upper,
                stated_confidence=b.stated_confidence,
                observed_accuracy=b.observed_accuracy,
                sample_size=b.sample_size,
            )
            for b in result.bins
        ],
        total_scored=result.total_scored,
        minimum_for_display=result.minimum_for_display,
        sufficient=result.sufficient,
        generated_at=datetime.now(tz=UTC),
    )
