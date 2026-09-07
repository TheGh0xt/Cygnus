import json

import pytest
from pydantic import ValidationError

from src.schemas.report import (
    CausalDriver,
    ClaimVerification,
    MarketAnalysisReport,
    MarketSource,
    SourceTier,
)

VALID = {
    "market_id": "0xabc",
    "timestamp": "2026-07-04T12:00:00Z",
    "summary": "Whale accumulation drove YES up 9 points.",
    "primary_causal_driver": "WHALE_ACTIVITY",
    "confidence_score": 0.85,
    "key_drivers": [
        {
            "type": "whale_trade",
            "impact": "HIGH",
            "evidence_summary": "$250k single-wallet buy",
        }
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
        MarketAnalysisReport.model_validate(
            {**VALID, "primary_causal_driver": "MOON_PHASE"}
        )


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


class TestSchemaV2:
    """The v2 additions, and the one property that must never regress."""

    def test_v1_payload_still_validates(self):
        """Rows written before v2 are already in the store.

        Both additions default, so an old blob loads without a migration. If
        this breaks, every stored report becomes unreadable and the accuracy
        record goes with it.
        """
        report = MarketAnalysisReport.model_validate(VALID)

        assert report.source is MarketSource.POLYMARKET
        assert report.cited_sources == []

    def test_cited_sources_carry_tier_and_verification(self):
        report = MarketAnalysisReport.model_validate(
            {
                **VALID,
                "cited_sources": [
                    {
                        "title": "FOMC statement",
                        "publisher": "federalreserve.gov",
                        "url": "https://example.invalid/fomc",
                        "published_at": "2026-09-06T18:00:00Z",
                        "tier": "PRIMARY",
                        "verification": "SUPPORTS",
                    },
                    {
                        "title": "Desk commentary",
                        "publisher": "unattributed aggregator",
                        "tier": "WEAK",
                        "verification": "CONTRADICTS",
                    },
                ],
            }
        )

        assert report.cited_sources[0].tier is SourceTier.PRIMARY
        assert report.cited_sources[1].verification is ClaimVerification.CONTRADICTS
        # A citation with no URL and no date is legitimate — a paywalled
        # excerpt has neither — and must not fail validation.
        assert report.cited_sources[1].url is None
        assert report.cited_sources[1].published_at is None

    def test_unknown_source_rejected(self):
        with pytest.raises(ValidationError):
            MarketAnalysisReport.model_validate({**VALID, "source": "BETFAIR"})

    def test_unknown_tier_rejected(self):
        with pytest.raises(ValidationError):
            MarketAnalysisReport.model_validate(
                {
                    **VALID,
                    "cited_sources": [
                        {
                            "title": "t",
                            "publisher": "p",
                            "tier": "GOLD",
                            "verification": "SUPPORTS",
                        }
                    ],
                }
            )

    def test_v2_dump_is_json_safe(self):
        """published_at is a second datetime on the JSON-safety path."""
        report = MarketAnalysisReport.model_validate(
            {
                **VALID,
                "cited_sources": [
                    {
                        "title": "t",
                        "publisher": "p",
                        "published_at": "2026-09-06T18:00:00Z",
                        "tier": "PRIMARY",
                        "verification": "SUPPORTS",
                    }
                ],
            }
        )
        dumped = report.model_dump()

        assert dumped["cited_sources"][0]["published_at"] == "2026-09-06T18:00:00+00:00"
        json.dumps(dumped)

    def test_report_carries_no_forecasting_fields(self):
        """PMIE explains moves; it does not forecast outcomes.

        A modelled probability of the market resolving, or an edge against the
        book, would turn this from research output into a trading signal —
        which UI_PRD and LIVE_MVP_PLAN both rule out. Adding such a field is a
        product decision, not a schema tweak, so it fails here first.
        """
        banned = {
            "edge",
            "p_yes",
            "brier_score",
            "model_probability",
            "modelled_probability",
            "modeled_probability",
            "fair_value",
        }

        assert not (banned & set(MarketAnalysisReport.model_fields))
