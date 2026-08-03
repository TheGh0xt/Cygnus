from datetime import UTC, datetime, timedelta

import pytest
from mcp.types import AudioContent, CallToolResult, TextContent

from src.evaluation.worker import (
    CONFIDENCE_DECREMENT,
    CONFIDENCE_INCREMENT,
    _extract_probability_from_tool_result,
    evaluate_report,
    run_evaluation_cycle,
)
from src.memory.store import SqliteMemoryStore
from src.schemas.report import MarketAnalysisReport

NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)


def make_report(confidence: float = 0.80, market_id: str = "0xabc") -> MarketAnalysisReport:
    return MarketAnalysisReport.model_validate(
        {
            "market_id": market_id,
            "timestamp": NOW.isoformat(),
            "summary": "Whale accumulation drove YES up 9 points.",
            "primary_causal_driver": "WHALE_ACTIVITY",
            "confidence_score": confidence,
            "key_drivers": [
                {
                    "type": "whale_trade",
                    "impact": "HIGH",
                    "evidence_summary": "$250k single-wallet buy",
                }
            ],
        }
    )


class TestEvaluateReport:
    def test_held_price_confirms(self):
        # Price at report 0.58, now 0.585 — held within tolerance.
        result = evaluate_report(make_report(0.80), price_at_report=0.58, current_price=0.585)
        assert result.outcome == "CONFIRMED"
        assert result.new_confidence == pytest.approx(0.80 + CONFIDENCE_INCREMENT)

    def test_extended_move_confirms(self):
        # Above-0.5 report price extended further upward.
        result = evaluate_report(make_report(0.80), price_at_report=0.58, current_price=0.70)
        assert result.outcome == "CONFIRMED"

    def test_extended_move_below_half_confirms(self):
        # Below-0.5 report price extended further downward (same-direction move).
        result = evaluate_report(make_report(0.80), price_at_report=0.30, current_price=0.20)
        assert result.outcome == "CONFIRMED"

    def test_reversal_decrements(self):
        # Price collapsed back toward 0.5 well beyond tolerance.
        result = evaluate_report(make_report(0.80), price_at_report=0.58, current_price=0.50)
        assert result.outcome == "REVERSED"
        assert result.new_confidence == pytest.approx(0.80 - CONFIDENCE_DECREMENT)

    def test_confidence_caps_at_one(self):
        result = evaluate_report(make_report(0.98), price_at_report=0.58, current_price=0.70)
        assert result.new_confidence == 1.0

    def test_confidence_floors_at_zero(self):
        result = evaluate_report(make_report(0.05), price_at_report=0.58, current_price=0.40)
        assert result.new_confidence == 0.0


class TestExtractProbabilityFromToolResult:
    def test_extracts_probability_from_text_content(self):
        result = CallToolResult(
            content=[TextContent(type="text", text='{"markets": [{"probability": 0.61}]}')]
        )

        assert _extract_probability_from_tool_result(result) == 0.61

    def test_skips_non_text_content(self):
        result = CallToolResult(
            content=[
                AudioContent(type="audio", data="", mimeType="audio/wav"),
                TextContent(type="text", text='{"markets": [{"probability": 0.61}]}'),
            ]
        )

        assert _extract_probability_from_tool_result(result) == 0.61

    @pytest.mark.parametrize(
        "result",
        [
            CallToolResult(content=[], isError=True),
            CallToolResult(content=[]),
            CallToolResult(content=[TextContent(type="text", text="not JSON")]),
            CallToolResult(content=[TextContent(type="text", text='{"markets": []}')]),
        ],
    )
    def test_rejects_invalid_or_unusable_results(self, result):
        assert _extract_probability_from_tool_result(result) is None


class FakeFetcher:
    def __init__(self, prices: dict[str, float | None]):
        self.prices = prices

    def current_probability(self, market_slug: str) -> float | None:
        return self.prices.get(market_slug)


class TestRunEvaluationCycle:
    @pytest.fixture
    def store(self, tmp_path):
        return SqliteMemoryStore(tmp_path / "memory.db")

    def test_due_reports_are_evaluated_and_recorded(self, store):
        store.save_report(make_report(0.80), "slug-a", 0.58, created_at=NOW)
        later = NOW + timedelta(hours=49)

        count = run_evaluation_cycle(store, FakeFetcher({"slug-a": 0.70}), now=later)

        assert count == 1
        assert store.get_reports_due_for_evaluation(later) == []
        stored = store.get_history_for_market("0xabc")[0]
        assert stored.outcome == "CONFIRMED"
        assert stored.report.confidence_score == pytest.approx(0.85)

    def test_unfetchable_markets_stay_due(self, store):
        store.save_report(make_report(0.80), "slug-gone", 0.58, created_at=NOW)
        later = NOW + timedelta(hours=49)

        count = run_evaluation_cycle(store, FakeFetcher({}), now=later)

        assert count == 0
        assert len(store.get_reports_due_for_evaluation(later)) == 1

    def test_young_reports_untouched(self, store):
        store.save_report(make_report(0.80), "slug-a", 0.58, created_at=NOW)

        count = run_evaluation_cycle(
            store, FakeFetcher({"slug-a": 0.70}), now=NOW + timedelta(hours=1)
        )

        assert count == 0
