import json

import pytest
from pydantic import ValidationError

from src.schemas.report import CausalDriver, MarketAnalysisReport

VALID = {
    "market_id": "0xabc",
    "timestamp": "2026-07-04T12:00:00Z",
    "summary": "Whale accumulation drove YES up 9 points.",
    "primary_causal_driver": "WHALE_ACTIVITY",
    "confidence_score": 0.85,
    "key_drivers": [
        {"type": "whale_trade", "impact": "HIGH", "evidence_summary": "$250k single-wallet buy"}
    ],
}


def test_valid_report_parses():
    report = MarketAnalysisReport.model_validate(VALID)
    assert report.primary_causal_driver is CausalDriver.WHALE_ACTIVITY
    assert report.historical_context_match is None


def test_summary_over_500_chars_rejected():
    with pytest.raises(ValidationError):
        MarketAnalysisReport.model_validate({**VALID, "summary": "x" * 501})


def test_confidence_out_of_range_rejected():
    for bad in (-0.1, 1.1):
        with pytest.raises(ValidationError):
            MarketAnalysisReport.model_validate({**VALID, "confidence_score": bad})


def test_unknown_driver_rejected():
    with pytest.raises(ValidationError):
        MarketAnalysisReport.model_validate({**VALID, "primary_causal_driver": "MOON_PHASE"})


def test_missing_required_field_rejected():
    incomplete = {k: v for k, v in VALID.items() if k != "key_drivers"}
    with pytest.raises(ValidationError):
        MarketAnalysisReport.model_validate(incomplete)


def test_model_dump_is_json_safe():
    """ADK writes model_dump() output straight into session state, which is then
    JSON-serialized — a raw datetime there blows up the session service."""
    dumped = MarketAnalysisReport.model_validate(VALID).model_dump(exclude_none=True)
    assert dumped["timestamp"] == "2026-07-04T12:00:00+00:00"
    json.dumps(dumped)


def test_json_round_trip():
    report = MarketAnalysisReport.model_validate(VALID)
    again = MarketAnalysisReport.model_validate(json.loads(report.model_dump_json()))
    assert again == report
