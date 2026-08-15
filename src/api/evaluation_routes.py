"""Triggering an evaluation cycle over HTTP.

Layer 5 scores each report against what the market actually did 48 hours
later. That only produces an accuracy record if it runs on a cadence, and the
cadence has to come from somewhere.

Why an endpoint rather than a scheduler inside the process: on a free
container plan the service is suspended when idle, so an in-process scheduler
simply does not fire. Platform cron is a paid feature. A scheduled GitHub
Actions workflow calling this endpoint costs nothing, runs whether or not the
service is awake, and the request itself wakes it.

Authentication is a shared secret rather than a user token, because the
caller is a machine with no user identity. The endpoint is closed unless
PMIE_CRON_SECRET is configured: a scoring run must never be triggerable by
an anonymous caller, both because it costs Sagittarius calls and because it
mutates the record the product's claims rest on.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os

from fastapi import APIRouter, Request

from ..evaluation.worker import run_evaluation_cycle
from .errors import ErrorType, PmieError
from .models import EvaluationRunResponse

logger = logging.getLogger("cygnus.api.evaluation")

router = APIRouter(prefix="/v1/internal", include_in_schema=False)

_SECRET_HEADER = "x-cron-secret"


def _authorise(request: Request) -> None:
    expected = os.getenv("PMIE_CRON_SECRET", "").strip()
    if not expected:
        # Closed by default. An unconfigured deployment must not expose a
        # trigger that spends money and rewrites scores.
        raise PmieError(
            ErrorType.INTERNAL_ERROR,
            "Scheduled evaluation is not configured on this server.",
            status=503,
        )

    provided = (request.headers.get(_SECRET_HEADER) or "").strip()
    # Constant-time comparison: a timing side channel on a long-lived shared
    # secret is worth avoiding even on an obscure endpoint.
    if not provided or not hmac.compare_digest(provided, expected):
        raise PmieError(
            ErrorType.INVALID_REQUEST, "Invalid or missing cron secret.", status=401
        )


@router.post("/evaluations/run", response_model=EvaluationRunResponse)
async def run_evaluations(request: Request) -> dict:
    """Score every report that has come due.

    The cycle runs to completion before responding. Each due report costs one
    Sagittarius call, and at current volumes a cycle is seconds; if the backlog
    ever grows past what a request can finish, this moves to a background task
    with the response reporting only that the run started.

    It runs in a worker thread rather than on the event loop. The cycle is
    synchronous and its price fetcher calls asyncio.run(), which raises
    RuntimeError if a loop is already running in that thread -- and raises it
    before the coroutine body executes, so the fetcher's own degraded path
    never gets the chance to turn the failure into "no price observed". The
    cycle also blocks on HTTP to both the report store and Sagittarius, so
    keeping it off the loop stops one scoring run from stalling every other
    request for its duration.
    """
    _authorise(request)

    store = request.app.state.memory_store
    prices = request.app.state.price_fetcher

    if store is None or prices is None:
        raise PmieError(
            ErrorType.INTERNAL_ERROR,
            "Evaluation dependencies are unavailable.",
            status=503,
        )

    try:
        evaluated = await asyncio.to_thread(run_evaluation_cycle, store, prices)
    except Exception as exc:
        # A failed cycle must be visible. Reports stay due and are retried on
        # the next run, so the cost of a failure is delay, not lost data.
        logger.exception("evaluation cycle failed")
        raise PmieError(
            ErrorType.INTERNAL_ERROR,
            "The evaluation cycle failed. Reports remain due for the next run.",
            status=500,
        ) from exc

    logger.info("evaluation cycle complete: %d report(s) scored", evaluated)
    return {"evaluated": evaluated}
