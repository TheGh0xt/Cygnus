from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .errors import ErrorType, PmieError
from .models import AnalysisCreated, AnalysisRequest, AnalysisResult, extract_slug

router = APIRouter(prefix="/v1")


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "cygnus"}


@router.get("/ready")
async def ready() -> dict:
    return {"status": "ready"}


@router.post("/analyses", status_code=201, response_model=AnalysisCreated)
async def create_analysis(body: AnalysisRequest, request: Request):
    registry = request.app.state.registry
    pipeline = request.app.state.pipeline
    record = registry.create(body.query)
    slug = extract_slug(body.query, body.slug)

    # Keep a reference: a bare create_task can be garbage-collected mid-run,
    # which would silently strand the analysis in "running".
    task = asyncio.create_task(pipeline.run(record.analysis_id, body.query, slug))
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
