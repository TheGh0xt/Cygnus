"""Triggering a generation cycle over HTTP — ROADMAP 4.12.

Layer 5 works and has scored. What it lacked was input: for six days nothing
created an analysis, so the accuracy record had a sample size of one. This
endpoint is what feeds it.

Same cadence mechanism as the evaluation trigger, and for the same reasons —
a free container plan suspends when idle so an in-process scheduler never
fires, and platform cron is paid. A scheduled GitHub Actions workflow calls
this, and the request itself wakes the service.

Two things make this endpoint stricter than its evaluation sibling:

  * It spends real money. Every generated analysis is four Gemini stages plus
    search grounding, so an unauthenticated caller could run up a bill, and a
    double-fired cron could do it by accident. Hence a server-side daily
    ceiling on top of the per-cycle limit.

  * It reports why nothing happened. The evaluation endpoint returns a bare
    count, and on 2026-08-20 that ambiguity — "nothing was due" versus "the
    fetch is broken" — survived in three documents for five days. This one
    returns the reasons.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, Request

from ..generation.worker import run_generation_cycle
from .errors import ErrorType, PmieError
from .models import GenerationRunResponse

logger = logging.getLogger("cygnus.api.generation")

router = APIRouter(prefix="/v1/internal", include_in_schema=False)

_SECRET_HEADER = "x-cron-secret"

# 8 analyses a day, as 2 per cycle across four 6-hourly cycles.
#
# Spread rather than bursted on purpose: reports created together come due
# together, and a batch that ages past several horizons between evaluation
# cycles gets backfilled from a single price observation — the defect seen on
# report 7, whose four checkpoints all share observed_price 0.074.
_DEFAULT_DAILY_CAP = 8
_DEFAULT_PER_CYCLE = 2


def _authorise(request: Request) -> None:
    expected = os.getenv("PMIE_CRON_SECRET", "").strip()
    if not expected:
        # Closed by default. An unconfigured deployment must not expose a
        # trigger that spends money.
        raise PmieError(
            ErrorType.INTERNAL_ERROR,
            "Scheduled generation is not configured on this server.",
            status=503,
        )

    provided = (request.headers.get(_SECRET_HEADER) or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise PmieError(
            ErrorType.INVALID_REQUEST, "Invalid or missing cron secret.", status=401
        )


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


@router.post("/generation/run", response_model=GenerationRunResponse)
async def run_generation(request: Request) -> dict:
    """Discover moving markets and analyse the most interesting of them."""
    _authorise(request)

    store = request.app.state.memory_store
    discovery = getattr(request.app.state, "discovery", None)
    pipeline = request.app.state.pipeline
    registry = request.app.state.registry

    if store is None or discovery is None or pipeline is None:
        raise PmieError(
            ErrorType.INTERNAL_ERROR,
            "Generation dependencies are unavailable.",
            status=503,
        )

    # A cap of 0 must mean zero, not "unlimited" — it is the kill switch.
    daily_cap = _int_env("PMIE_GENERATION_DAILY_CAP", _DEFAULT_DAILY_CAP)
    per_cycle = _int_env("PMIE_GENERATION_PER_CYCLE", _DEFAULT_PER_CYCLE)

    try:
        report = await run_generation_cycle(
            store=store,
            discovery=discovery,
            pipeline=pipeline,
            registry=registry,
            limit=per_cycle,
            daily_cap=daily_cap,
        )
    except Exception as exc:
        logger.exception("generation cycle failed")
        raise PmieError(
            ErrorType.INTERNAL_ERROR,
            "The generation cycle failed. No analyses were written.",
            status=500,
        ) from exc

    return report.as_dict()
