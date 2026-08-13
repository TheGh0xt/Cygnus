from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .accounts import FREE_MONTHLY_ANALYSES, AccountsError
from .dependencies import current_user, require_invited
from .errors import ErrorType, PmieError
from .models import (
    AnalysisCreated,
    AnalysisRequest,
    AnalysisResult,
    CategoriesResponse,
    FeedbackRequest,
    HealthResponse,
    InterestsRequest,
    InterestsResponse,
    MeResponse,
    ProblemResponse,
    UsageSummary,
    extract_slug,
)
from .ratelimit import RateLimitExceeded

# Every failure path returns problem+json. Declaring it here means /docs shows
# the real error shape instead of FastAPI's default validation schema.
PROBLEM = {
    "model": ProblemResponse,
    "content": {"application/problem+json": {}},
}
_AUTH_ERRORS = {
    401: {"description": "Not signed in, or the token failed verification", **PROBLEM},
    403: {"description": "Signed in but not invited to the alpha", **PROBLEM},
    503: {"description": "A dependency is unavailable", **PROBLEM},
}

router = APIRouter(prefix="/v1")


@router.get("/health", response_model=HealthResponse, summary="Liveness")
async def health() -> dict:
    """Answers as soon as the process is up. Public: load balancers cannot
    authenticate."""
    return {"status": "ok", "service": "cygnus"}


@router.get("/ready", response_model=HealthResponse, summary="Readiness")
async def ready() -> dict:
    return {"status": "ready"}


@router.post(
    "/analyses",
    status_code=201,
    response_model=AnalysisCreated,
    summary="Start an analysis",
    responses={
        **_AUTH_ERRORS,
        429: {"description": "Rate limit exceeded", **PROBLEM},
    },
)
async def create_analysis(body: AnalysisRequest, request: Request):
    """Starts a run and returns immediately with a stream URL.

    A full analysis takes 60-120 seconds across four stages, so the work
    happens in the background and progress arrives over the SSE endpoint.
    """
    user = current_user(request)
    require_invited(request, user)

    # After the invite check, so an uninvited caller is told that rather than
    # being rate limited, and before any work starts — this endpoint is the
    # expensive one and the limit exists to protect the model budget.
    try:
        request.app.state.limiter.check(user.id)
    except RateLimitExceeded as exc:
        raise PmieError(
            ErrorType.RATE_LIMITED,
            f"Too many analyses. Try again in {exc.retry_after} seconds.",
            status=429,
        ) from exc

    registry = request.app.state.registry
    pipeline = request.app.state.pipeline
    record = registry.create(body.query)
    slug = extract_slug(body.query, body.slug)

    # Keep a reference: a bare create_task can be garbage-collected mid-run,
    # which would silently strand the analysis in "running".
    task = asyncio.create_task(
        pipeline.run(record.analysis_id, body.query, slug, profile_id=user.id)
    )
    request.app.state.tasks.add(task)
    task.add_done_callback(request.app.state.tasks.discard)

    return AnalysisCreated(
        analysis_id=record.analysis_id,
        stream_url=f"/v1/analyses/{record.analysis_id}/events",
        status=record.status.value,
    )


@router.get("/analyses/{analysis_id}", response_model=AnalysisResult)
async def get_analysis(analysis_id: str, request: Request):
    record = request.app.state.registry.get(analysis_id)
    if record is None:
        raise PmieError(
            ErrorType.ANALYSIS_NOT_FOUND, f"no analysis {analysis_id}", status=404
        )
    return AnalysisResult(
        analysis_id=record.analysis_id,
        status=record.status.value,
        report=record.report,
        error=record.error,
    )


@router.get(
    "/analyses/{analysis_id}/events",
    # OpenAPI cannot describe an SSE frame sequence, but it can at least stop
    # claiming this returns JSON. Client generators key off the media type.
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "Server-sent events. One `stage_started` / `stage_completed` "
                "pair per pipeline stage (event_retrieval, signal_retrieval, "
                "news_retrieval, analysis), then a terminal `report` or "
                "`error` event. Each `data:` line is a JSON object "
                "`{stage, data}`."
            ),
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def stream_analysis(analysis_id: str, request: Request):
    record = request.app.state.registry.get(analysis_id)
    if record is None:
        raise PmieError(
            ErrorType.ANALYSIS_NOT_FOUND, f"no analysis {analysis_id}", status=404
        )

    async def generator():
        while True:
            item = await record.queue.get()
            if item is None:
                break
            payload = json.dumps({"stage": item.stage, "data": item.data})
            yield f"event: {item.event}\ndata: {payload}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/me",
    response_model=MeResponse,
    summary="The signed-in user",
    responses=_AUTH_ERRORS,
)
async def me(request: Request) -> dict:
    """The caller's profile, interests and usage.

    One call so the client can render the whole authenticated shell — header,
    usage indicator, onboarding state — without a waterfall of requests.
    """
    user = current_user(request)
    accounts = request.app.state.accounts
    if not accounts.configured:
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Accounts are not configured.", status=503
        )

    try:
        profile = accounts.get_profile(user.id)
        interests = accounts.get_interests(user.id) if profile else []
        used = accounts.monthly_usage(user.id) if profile else 0
    except AccountsError as exc:
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Could not load your account.", status=503
        ) from exc

    if profile is None:
        raise PmieError(
            ErrorType.INVALID_REQUEST, "No account found for this session.", status=401
        )

    return {
        "id": profile.id,
        "email": user.email,
        "display_name": profile.display_name,
        "is_invited": profile.is_invited,
        "is_grandfathered": profile.is_grandfathered,
        "onboarding_completed": profile.onboarding_completed_at is not None,
        "interests": interests,
        "usage": UsageSummary(
            analyses_this_month=used,
            free_monthly_allowance=FREE_MONTHLY_ANALYSES,
        ),
    }


@router.get(
    "/interests/categories",
    response_model=CategoriesResponse,
    summary="Selectable market categories",
    responses={503: _AUTH_ERRORS[503]},
)
async def interest_categories(request: Request) -> dict:
    """The selectable categories.

    Served from the database rather than hardcoded in the client so the list
    and its labels have one source of truth.
    """
    accounts = request.app.state.accounts
    if not accounts.configured:
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Accounts are not configured.", status=503
        )
    try:
        return {"categories": accounts.list_categories()}
    except AccountsError as exc:
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Could not load categories.", status=503
        ) from exc


@router.put(
    "/me/interests",
    response_model=InterestsResponse,
    summary="Choose 3-5 interest categories",
    responses={
        **_AUTH_ERRORS,
        422: {
            "description": "Fewer than 3, more than 5, or unknown categories",
            **PROBLEM,
        },
    },
)
async def set_interests(body: InterestsRequest, request: Request) -> dict:
    user = current_user(request)
    accounts = request.app.state.accounts
    try:
        saved = accounts.set_interests(user.id, body.categories)
    except AccountsError as exc:
        raise PmieError(ErrorType.INVALID_REQUEST, str(exc), status=422) from exc
    return {"interests": saved}


@router.post(
    "/analyses/{analysis_id}/feedback",
    status_code=204,
    summary="Rate a report",
    responses=_AUTH_ERRORS,
)
async def submit_feedback(
    analysis_id: str, body: FeedbackRequest, request: Request
) -> None:
    """Useful / not useful on a report.

    Closes the gap left by Phase 1: the API contract listed this endpoint but
    it was never built, so the UI had nothing to call.
    """
    user = current_user(request)
    accounts = request.app.state.accounts
    try:
        accounts.save_feedback(user.id, analysis_id, body.is_useful, body.note)
    except AccountsError as exc:
        raise PmieError(
            ErrorType.INTERNAL_ERROR, "Could not save your feedback.", status=503
        ) from exc
