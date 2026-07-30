"""Rigid output contract for the reasoning pipeline.

Mirrors the MarketAnalysisReport JSON schema in docs/docs_AGENT_SPEC.md
section 2 — downstream services and the evaluation engine depend on this
shape being stable and type-checked.
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


class Impact(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class KeyDriver(BaseModel):
    type: str
    impact: Impact
    evidence_summary: str


class HistoricalContextMatch(BaseModel):
    previous_market_id: str
    prior_explanation_accuracy: float


class MarketAnalysisReport(BaseModel):
    market_id: str
    timestamp: datetime
    summary: str = Field(max_length=500)
    primary_causal_driver: CausalDriver
    confidence_score: float = Field(ge=0.0, le=1.0)
    key_drivers: list[KeyDriver]
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
