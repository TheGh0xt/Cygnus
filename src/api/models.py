from __future__ import annotations

import re

from pydantic import BaseModel, Field

_URL_SLUG = re.compile(r"polymarket\.com/event/([a-z0-9-]+)", re.IGNORECASE)
_BARE_SLUG = re.compile(r"\b([a-z0-9]+(?:-[a-z0-9]+){2,})\b")


class AnalysisRequest(BaseModel):
    query: str = Field(min_length=3, max_length=800)
    slug: str | None = None


class AnalysisCreated(BaseModel):
    analysis_id: str
    stream_url: str
    status: str


class AnalysisResult(BaseModel):
    analysis_id: str
    status: str
    report: dict | None = None
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
