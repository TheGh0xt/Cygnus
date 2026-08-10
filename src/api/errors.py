"""RFC 9457 problem+json error model.

Clients switch on `type`, never on `title` or `detail` — the slug is the
stable contract, the prose is free to change.
"""

from __future__ import annotations

from enum import Enum

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .logging import request_id_var

_BASE = "https://pmie.dev/problems/"


class ErrorType(str, Enum):
    SAGITTARIUS_UNAVAILABLE = "sagittarius-unavailable"
    EVENT_NOT_FOUND = "event-not-found"
    MODEL_ERROR = "model-error"
    RATE_LIMITED = "rate-limited"
    QUOTA_EXCEEDED = "quota-exceeded"
    INVALID_REQUEST = "invalid-request"
    ANALYSIS_NOT_FOUND = "analysis-not-found"
    INTERNAL_ERROR = "internal-error"


_TITLES = {
    ErrorType.SAGITTARIUS_UNAVAILABLE: "Market data service unavailable",
    ErrorType.EVENT_NOT_FOUND: "Event not found",
    ErrorType.MODEL_ERROR: "Reasoning model error",
    ErrorType.RATE_LIMITED: "Rate limit exceeded",
    ErrorType.QUOTA_EXCEEDED: "Quota exceeded",
    ErrorType.INVALID_REQUEST: "Invalid request",
    ErrorType.ANALYSIS_NOT_FOUND: "Analysis not found",
    ErrorType.INTERNAL_ERROR: "Internal error",
}


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    request_id: str | None = None


class PmieError(Exception):
    def __init__(self, error_type: ErrorType, detail: str, status: int) -> None:
        super().__init__(detail)
        self.error_type = error_type
        self.detail = detail
        self.status = status


def problem_response(exc: PmieError, instance: str | None = None) -> JSONResponse:
    problem = Problem(
        type=f"{_BASE}{exc.error_type.value}",
        title=_TITLES[exc.error_type],
        status=exc.status,
        detail=exc.detail,
        instance=instance,
        request_id=request_id_var.get() or None,
    )
    return JSONResponse(
        status_code=exc.status,
        content=problem.model_dump(),
        media_type="application/problem+json",
    )
