"""Rigid output contract for the reasoning pipeline.

Mirrors the MarketAnalysisReport JSON schema in docs/docs_AGENT_SPEC.md
section 2 — downstream services and the evaluation engine depend on this
shape being stable and type-checked.

Schema v2 (2026-09-07) adds three things, all optional with defaults so that
v1 rows already in the memory store still validate:

  * `source` — which venue the market lives on. Polymarket is the only one
    wired today; the field exists so adding Kalshi is additive rather than a
    migration of every stored report.
  * `cited_sources` — the evidence ledger, each item carrying a provenance
    tier and whether it supports or contradicts the stated cause. This is
    what separates "here are five articles" from "three support this thesis
    and one contradicts it".

There is deliberately no `schema_version` field. Both additions default, so a
v1 row validates as-is — and a version stamp in the body would have to claim
v2 for rows written before v2 existed. If the store ever needs to tell the
shapes apart, that belongs in a column beside the blob, not inside it. It
would also cost tokens on every analysis, since the analyst's output_schema
puts Gemini in JSON mode against this exact model.

Deliberately NOT here: any modelled probability of the market resolving, and
any edge against the book. PMIE explains moves and grades its own
explanations; it does not forecast outcomes, and a field implying otherwise
would make it a trading signal.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_serializer


class CausalDriver(str, Enum):
    WHALE_ACTIVITY = "WHALE_ACTIVITY"
    VOLUME_SPIKE = "VOLUME_SPIKE"
    LIQUIDITY_CRUNCH = "LIQUIDITY_CRUNCH"
    EXTERNAL_NEWS = "EXTERNAL_NEWS"
    UNKNOWN_ANOMALY = "UNKNOWN_ANOMALY"


class MarketSource(str, Enum):
    POLYMARKET = "POLYMARKET"
    KALSHI = "KALSHI"


class SourceTier(str, Enum):
    """How much weight a citation can carry on its own."""

    PRIMARY = "PRIMARY"  # the issuing body: a filing, a statement, an official release
    PARTIAL = "PARTIAL"  # secondhand but attributed, or an excerpt behind a paywall
    WEAK = "WEAK"  # unattributed aggregation, commentary, or an unnamed source


class ClaimVerification(str, Enum):
    """Whether a citation bears on the stated cause — not whether it is true."""

    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    UNSUPPORTED = "UNSUPPORTED"  # retrieved, but says nothing about this move


class Impact(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class KeyDriver(BaseModel):
    type: str
    impact: Impact
    evidence_summary: str


class CitedSource(BaseModel):
    """One item of evidence, with its provenance and its bearing on the cause.

    A CONTRADICTS entry is a feature, not a failure: showing the evidence that
    cuts against the conclusion is what makes the confidence score legible.
    """

    title: str
    publisher: str
    url: str | None = None
    published_at: datetime | None = None
    tier: SourceTier
    verification: ClaimVerification

    @field_serializer("published_at")
    def _serialize_published_at(self, value: datetime | None) -> str | None:
        return value.isoformat() if value else None


class HistoricalContextMatch(BaseModel):
    previous_market_id: str
    prior_explanation_accuracy: float


class MarketAnalysisReport(BaseModel):
    market_id: str
    source: MarketSource = MarketSource.POLYMARKET
    timestamp: datetime
    summary: str = Field(max_length=500)
    primary_causal_driver: CausalDriver
    confidence_score: float = Field(ge=0.0, le=1.0)
    key_drivers: list[KeyDriver]
    cited_sources: list[CitedSource] = []
    historical_context_match: HistoricalContextMatch | None = None

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        """Keep `model_dump()` JSON-safe in python mode too.

        ADK validates the analyst's reply and writes `model_dump()` straight
        into session state, which the session service then JSON-serializes —
        a raw datetime there raises "Object of type datetime is not JSON
        serializable". Validation on input still enforces a real timestamp.
        """
        return value.isoformat()
