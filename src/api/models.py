from __future__ import annotations

import re
from datetime import datetime
from enum import Enum

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


class GenerationSkip(BaseModel):
    market_slug: str
    reason: str


class GenerationRunResponse(BaseModel):
    """What one generation cycle did, and why it did not do more.

    Deliberately richer than EvaluationRunResponse. A bare count cannot
    distinguish "nothing was moving" from "Sagittarius is down" from "the
    spend ceiling stopped it", and on 2026-08-20 exactly that ambiguity went
    undiagnosed for five days.
    """

    generated: int
    no_candidates: bool = False
    discovery_error: str | None = None
    skipped: list[GenerationSkip] = []
    skipped_by_reason: dict[str, int] = {}


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


# ─────────────────────────────────────────────────────────────────────────────
# Frozen contract — shapes agreed before the logic exists.
#
# These models are the whole point of Wave 0: Lyra generates its typed client
# from openapi.json, so a screen cannot be built against a route that is not in
# the schema. Freezing the shapes up front lets the three repos work in
# parallel against a committed artifact instead of negotiating with each other
# mid-build. The routes serving them return 501 until their owning team lands
# the logic; see contract_stubs.py.
# ─────────────────────────────────────────────────────────────────────────────


class MfaEnrollResponse(BaseModel):
    """TOTP enrollment. The secret is shown exactly once."""

    factor_id: str
    secret: str = Field(description="Base32 TOTP secret. Never returned again.")
    qr_uri: str = Field(description="otpauth:// URI for a QR code.")
    recovery_codes: list[str] = Field(
        description="Single-use fallbacks. Shown once, stored hashed."
    )


class MfaVerifyRequest(BaseModel):
    factor_id: str
    code: str = Field(min_length=6, max_length=8)


class MfaStatusResponse(BaseModel):
    enrolled: bool
    verified_at: datetime | None = None


class ReferralSummary(BaseModel):
    code: str = Field(description="This user's own referral code.")
    referred_count: int
    converted_count: int = Field(
        description="Referrals that verified their email. Only these count."
    )
    analyses_granted: int
    next_reward_at: int = Field(
        description="Converted referrals needed for the next grant."
    )


class WaitlistRequest(BaseModel):
    email: str
    referral_code: str | None = None


class WaitlistResponse(BaseModel):
    position: int | None = Field(
        default=None, description="Null when positions are not disclosed."
    )
    already_registered: bool = False


class PayIntentRequest(BaseModel):
    """A click on the quota wall, not a payment.

    No money moves during beta. This records that someone who had used the
    product wanted more of it at a stated price, which is the signal the beta
    exists to collect.
    """

    price_shown_usd: float = Field(ge=0, description="The price on screen.")
    plan: str = Field(description="Plan label shown, e.g. 'pro-monthly'.")


class UiMode(str, Enum):
    TERMINAL = "TERMINAL"
    CONVENTIONAL = "CONVENTIONAL"


class UserEventRequest(BaseModel):
    """Product telemetry. Never PII beyond the authenticated user id."""

    name: str = Field(description="e.g. 'ui_mode_switched', 'analysis_started'.")
    ui_mode: UiMode | None = None
    properties: dict[str, str] = {}


class ShareTokenResponse(BaseModel):
    token: str
    url: str
    expires_at: datetime | None = None


class CalibrationBin(BaseModel):
    """One point on the reliability curve."""

    lower: float = Field(ge=0.0, le=1.0)
    upper: float = Field(ge=0.0, le=1.0)
    stated_confidence: float = Field(description="Mean confidence claimed in this bin.")
    observed_accuracy: float = Field(description="Share that held up at 48h.")
    sample_size: int


class CalibrationResponse(BaseModel):
    """Explanation calibration — NOT a forecast record.

    This answers "when PMIE says 0.7, is it right about 70% of the time?" It
    does not score predictions of market outcomes, because PMIE does not make
    any. `sufficient` gates display: a curve drawn from a handful of reports
    misleads, so the UI shows a collecting state until there is enough.
    """

    bins: list[CalibrationBin]
    total_scored: int
    minimum_for_display: int
    sufficient: bool
    generated_at: datetime


class MovingMarket(BaseModel):
    slug: str
    question: str
    source: str = Field(description="POLYMARKET or KALSHI.")
    probability: float = Field(ge=0.0, le=1.0)
    change_24h: float
    volume_24h: float
    category: str
    days_to_resolution: int | None = None


class MovingMarketsResponse(BaseModel):
    """The personalised feed — ranked by movement, not popularity (UI_PRD 6.4)."""

    markets: list[MovingMarket]
    categories: list[str] = Field(description="Categories this feed was built from.")
