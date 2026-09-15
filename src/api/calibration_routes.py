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

import time
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from ..evaluation.calibration import compute_calibration
from .models import CalibrationBin, CalibrationResponse

router = APIRouter(prefix="/v1")

# A few minutes is invisible on an aggregate trust signal that only moves as
# reports get scored at T+48h, and bounds the cost of an unauthenticated
# route reading every scored row on every hit (B.11 review).
CACHE_TTL_SECONDS = 180.0


class CalibrationCache:
    """Caches the narrow (stated_confidence, outcome) read behind the curve.

    One instance per app (see create_app), not a module-level global: a
    global would leak state between the independent apps each test's
    `create_app()` builds. Same shape as auth.JwksCache.
    """

    def __init__(self, ttl_seconds: float = CACHE_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._pairs: list[tuple[float, str]] | None = None
        self._fetched_at = 0.0

    def get(self, store) -> list[tuple[float, str]]:
        fresh = time.time() - self._fetched_at < self._ttl
        if self._pairs is not None and fresh:
            return self._pairs
        # No store configured (e.g. a from-scratch dev environment) reads the
        # same as "nothing scored yet" — the insufficient-data state, not an
        # error. A trust-signal endpoint that 500s is worse than one that
        # says "still collecting".
        self._pairs = (
            store.get_scored_confidence_outcomes() if store is not None else []
        )
        self._fetched_at = time.time()
        return self._pairs


@router.get(
    "/calibration",
    response_model=CalibrationResponse,
    summary="Reliability of PMIE's own confidence scores",
)
def calibration(request: Request) -> CalibrationResponse:
    # Plain `def`: the store call below is blocking (sync httpx on Postgres),
    # so an `async def` here would run it inline on the event loop and stall
    # every concurrent SSE stream — the same F1 pattern fixed in #26/#32.
    store = request.app.state.memory_store
    pairs = request.app.state.calibration_cache.get(store)

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
