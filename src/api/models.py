from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..schemas.report import MarketAnalysisReport

_URL_SLUG = re.compile(r"polymarket\.com/event/([a-z0-9-]+)", re.IGNORECASE)
_BARE_SLUG = re.compile(r"\b([a-z0-9]+(?:-[a-z0-9]+){2,})\b")


class AnalysisRequest(BaseModel):
    query: str = Field(min_length=3, max_length=800)
    slug: str | None = None


class AnalysisCreated(BaseModel):
    analysis_id: str
    stream_url: str
    status: str


class InterestCategory(BaseModel):
    slug: str
    label: str
    description: str | None = None
    sort_order: int = 0


class CategoriesResponse(BaseModel):
    categories: list[InterestCategory]


class InterestsResponse(BaseModel):
    interests: list[str]


class UsageSummary(BaseModel):
    analyses_this_month: int
    free_monthly_allowance: int
    # Reported, not enforced. Pricing stays gated behind a published accuracy
    # record, so today this only tells a user where they stand.
    enforced: bool = False


class MeResponse(BaseModel):
    id: str
    email: str | None = None
    display_name: str | None = None
    is_invited: bool
    is_grandfathered: bool
    onboarding_completed: bool
    interests: list[str]
    usage: UsageSummary


class HealthResponse(BaseModel):
    status: str
    service: str | None = None


class ReadyChecks(BaseModel):
    # "ok" | "failed" | "unknown" — unknown when Supabase is not configured,
    # which is the normal local-development case.
    report_store_writable: str
    detail: str | None = None


class ReadyResponse(BaseModel):
    status: str
    service: str | None = None
    checks: ReadyChecks


class EvaluationRunResponse(BaseModel):
    """The outcome of one scoring cycle.

    `evaluated` alone cannot distinguish a cycle with nothing to do from one
    that could reach nothing, so the caller gets the denominator and the
    failure count too.
    """

    evaluated: int
    reports_due: int = 0
    price_unavailable: int = 0
    degraded: bool = False


class ProblemResponse(BaseModel):
    """RFC 9457 problem+json.

    Declared so the documented error shape matches what the API actually
    returns; clients switch on `type`, never on `title` or `detail`.
    """

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    request_id: str | None = None


class InterestsRequest(BaseModel):
    # Range is enforced in the accounts layer, not here, so the API can return
    # one consistent message whether the list is too short, too long, or full
    # of duplicates that collapse below the minimum.
    categories: list[str] = Field(min_length=1, max_length=20)


class FeedbackRequest(BaseModel):
    is_useful: bool
    note: str | None = Field(default=None, max_length=2000)


class AnalysisResult(BaseModel):
    analysis_id: str
    status: str
    # Typed rather than a bare dict so MarketAnalysisReport lands in
    # openapi.json. A `dict` here serialises as an untyped object, which
    # would leave the generated UI client with no types for the one payload
    # that actually matters — drivers, confidence, evidence.
    report: MarketAnalysisReport | None = None
    error: str | None = None


def extract_slug(query: str, slug: str | None) -> str:
    """Resolve the event slug the analysis is about.

    Explicit field wins; then a Polymarket URL; then the longest hyphenated
    token, which is what a slug looks like inside a natural-language
    question. Returns "" when nothing looks like a slug — the pipeline still
    runs, the report is just stored without one.
    """
    if slug:
        return slug
    url_match = _URL_SLUG.search(query)
    if url_match:
        return url_match.group(1)
    candidates = _BARE_SLUG.findall(query.lower())
    return max(candidates, key=len) if candidates else ""
