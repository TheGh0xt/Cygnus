import json
import logging
from datetime import UTC, datetime

import pytest

from src.api.pipeline import (
    AnalysisPipeline,
    _citation_coverage_violations,
    has_no_usable_market_data,
    stage_for_author,
)
from src.api.registry import AnalysisRegistry, AnalysisStatus


def test_known_authors_map_to_public_stage_names():
    assert stage_for_author("analysis_event_retrieval") == "event_retrieval"
    assert stage_for_author("analysis_signal_retrieval") == "signal_retrieval"
    assert stage_for_author("analysis_news_retrieval") == "news_retrieval"
    assert stage_for_author("market_analyst_agent") == "analysis"


def test_unknown_author_has_no_public_stage():
    # Internal agents must not leak into the public event stream.
    assert stage_for_author("polymarket_orchestrator") is None


class FakeActions:
    """Mirrors ADK's EventActions: output_key writes arrive as state_delta."""

    def __init__(self, state_delta=None):
        self.state_delta = state_delta or {}


class FakeFunctionResponse:
    """Mirrors google.genai.types.FunctionResponse enough for get_function_responses()."""

    def __init__(self, response):
        self.response = response


def _mcp_envelope(*payloads):
    """Wraps JSON payloads the way Sagittarius's TextContent actually does:
    {"content": [{"type": "text", "text": "<json>"}]} — never structuredContent
    (internal/interface/mcp/tools/*.go), so a decoder must go through the text
    field, not read the dict directly."""
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload)} for payload in payloads
        ]
    }


class FakeEvent:
    def __init__(self, author, report=None, final=False, tool_payloads=None):
        self.author = author
        self.actions = FakeActions({"market_analysis_report": report} if report else {})
        self._final = final or report is not None
        self._tool_payloads = tool_payloads

    def is_final_response(self):
        return self._final

    def get_function_responses(self):
        if self._tool_payloads is None:
            return []
        return [FakeFunctionResponse(_mcp_envelope(*self._tool_payloads))]


def _event_with_markets(author, markets, final=False):
    return FakeEvent(author, final=final, tool_payloads=[{"markets": markets}])


class FakeSessionService:
    @staticmethod
    async def create_session(**kwargs):
        class S:
            id = kwargs.get("session_id", "s1")

        return S()


class FakeRunner:
    session_service = FakeSessionService()

    async def run_async(self, **kwargs):
        yield FakeEvent("analysis_event_retrieval")
        yield FakeEvent("market_analyst_agent", report={"market_id": "0xabc"})


@pytest.mark.asyncio
async def test_pipeline_publishes_stage_events_and_completes():
    registry = AnalysisRegistry()
    record = registry.create("why did it move?")
    pipeline = AnalysisPipeline(registry, FakeRunner())
    await pipeline.run(record.analysis_id, "why did it move?", "world-cup-winner")

    assert registry.get(record.analysis_id).status is AnalysisStatus.COMPLETED

    events = []
    while not record.queue.empty():
        events.append(record.queue.get_nowait())
    kinds = [e.event for e in events if e is not None]
    assert "stage_started" in kinds
    assert "report" in kinds


@pytest.mark.asyncio
async def test_pipeline_marks_failure_and_closes_stream():
    class Boom:
        session_service = FakeSessionService()

        async def run_async(self, **kwargs):
            raise RuntimeError("sagittarius down")
            yield  # unreachable; makes this an async generator

    registry = AnalysisRegistry()
    record = registry.create("q")
    await AnalysisPipeline(registry, Boom()).run(record.analysis_id, "q", "slug")

    assert registry.get(record.analysis_id).status is AnalysisStatus.FAILED
    drained = []
    while not record.queue.empty():
        drained.append(record.queue.get_nowait())
    assert drained[-1] is None  # stream always terminates
    assert any(e is not None and e.event == "error" for e in drained)


class TestCitationCoverageViolations:
    """B.12 review: the analyst prompt asks Gemini to populate cited_sources
    with tier/verification tags, but nothing enforces that a model actually
    does — a report that ignores the instructions is still schema-valid with
    cited_sources == []. This is the deterministic check that catches that,
    after the fact, without failing the analysis."""

    def _report(self, driver="WHALE_ACTIVITY", cited_sources=None):
        return {
            "primary_causal_driver": driver,
            "cited_sources": cited_sources or [],
        }

    def test_no_violation_when_citations_present_for_real_news(self):
        report = self._report(
            cited_sources=[{"title": "x", "publisher": "y", "verification": "SUPPORTS"}]
        )
        assert _citation_coverage_violations(report, "FOMC raises rates...") == []

    def test_violation_when_real_news_but_no_citations(self):
        violations = _citation_coverage_violations(
            self._report(), "FOMC raises rates..."
        )
        assert len(violations) == 1
        assert "cited_sources is empty" in violations[0]

    def test_no_violation_when_news_is_the_sentinel(self):
        assert _citation_coverage_violations(self._report(), "NO_RELEVANT_NEWS") == []

    def test_no_violation_when_news_context_is_absent(self):
        assert _citation_coverage_violations(self._report(), None) == []

    def test_violation_when_external_news_driver_has_no_supporting_citation(self):
        report = self._report(
            driver="EXTERNAL_NEWS",
            cited_sources=[
                {"title": "x", "publisher": "y", "verification": "CONTRADICTS"}
            ],
        )
        violations = _citation_coverage_violations(report, "some item")
        assert any("EXTERNAL_NEWS" in v for v in violations)

    def test_no_violation_when_external_news_driver_has_a_supporting_citation(self):
        report = self._report(
            driver="EXTERNAL_NEWS",
            cited_sources=[
                {"title": "x", "publisher": "y", "verification": "SUPPORTS"}
            ],
        )
        assert _citation_coverage_violations(report, "some item") == []

    def test_both_rules_can_fire_together(self):
        violations = _citation_coverage_violations(
            self._report(driver="EXTERNAL_NEWS"), "some item"
        )
        assert len(violations) == 2


@pytest.mark.asyncio
async def test_pipeline_logs_citation_coverage_violations_without_failing(caplog):
    class NewsThenReport:
        session_service = FakeSessionService()

        async def run_async(self, **kwargs):
            yield FakeEvent(
                "analysis_news_retrieval",
                report=None,
                final=True,
            )
            # FakeEvent's report kwarg only ever writes market_analysis_report;
            # a second delta is layered on to also carry the news digest.
            news_event = FakeEvent("analysis_news_retrieval")
            news_event.actions = FakeActions(
                {"news_context_output": "Fed raises rates"}
            )
            yield news_event
            yield FakeEvent(
                "market_analyst_agent",
                report={
                    "market_id": "0xabc",
                    "primary_causal_driver": "WHALE_ACTIVITY",
                    "cited_sources": [],
                },
            )

    registry = AnalysisRegistry()
    record = registry.create("why did it move?")
    with caplog.at_level(logging.WARNING, logger="cygnus.api.pipeline"):
        await AnalysisPipeline(registry, NewsThenReport()).run(
            record.analysis_id, "why did it move?", "world-cup-winner"
        )

    # The check must never fail the run — it only flags a quality gap.
    assert registry.get(record.analysis_id).status is AnalysisStatus.COMPLETED
    assert any("cited_sources is empty" in r.message for r in caplog.records)


class TestHasNoUsableMarketData:
    """F15: a total Sagittarius outage or an empty snapshot must be
    detected from the retrieval stages' raw MCP tool JSON, not from the
    agents' text restatement of it — trusting a model's paraphrase of "no
    data" instead of the fact itself is the same class of mistake that
    produced F15's confident-looking report in the first place."""

    def test_true_when_no_markets_at_all(self):
        events = [_event_with_markets("analysis_event_retrieval", [])]
        assert has_no_usable_market_data(events) is True

    def test_true_when_no_tool_payloads_reported(self):
        # Sagittarius unreachable: the retrieval agent's own text may say
        # "no data found", but there is no tool JSON to even parse.
        assert has_no_usable_market_data([FakeEvent("analysis_event_retrieval")]) is True

    def test_true_when_condition_id_present_but_every_probability_is_zero(self):
        events = [
            _event_with_markets(
                "analysis_event_retrieval", [{"condition_id": "0xabc", "probability": 0.0}]
            ),
            _event_with_markets(
                "analysis_signal_retrieval", [{"condition_id": "0xabc", "probability": 0.0}]
            ),
        ]
        assert has_no_usable_market_data(events) is True

    def test_false_when_a_market_has_a_real_nonzero_probability(self):
        events = [
            _event_with_markets(
                "analysis_event_retrieval", [{"condition_id": "0xabc", "probability": 0.42}]
            )
        ]
        assert has_no_usable_market_data(events) is False

    def test_false_for_a_real_quiet_market_with_zero_volume(self):
        # The coordinator's correction: a resolved probability with zero
        # volume is a legitimately quiet market, not an outage. Volume must
        # never be part of this gate, however it's spelled.
        events = [
            _event_with_markets(
                "analysis_event_retrieval",
                [{"condition_id": "0xabc", "probability": 0.5, "volume_24h": 0.0}],
            ),
            _event_with_markets(
                "analysis_signal_retrieval",
                [
                    {
                        "condition_id": "0xabc",
                        "probability": 0.5,
                        "volume_analysis": {"velocity": 0.0},
                        "whale_count": 0,
                    }
                ],
            ),
        ]
        assert has_no_usable_market_data(events) is False

    def test_false_when_some_markets_have_real_data(self):
        events = [
            _event_with_markets(
                "analysis_event_retrieval",
                [
                    {"condition_id": "0xabc", "probability": 0.0},
                    {"condition_id": "0xdef", "probability": 0.61},
                ],
            )
        ]
        assert has_no_usable_market_data(events) is False

    def test_true_when_tool_text_is_unparseable(self):
        # Malformed/non-JSON tool text yields no markets, which is flavour
        # (a) — never crash trying to make sense of it.
        event = FakeEvent("analysis_event_retrieval")
        event.get_function_responses = lambda: [
            FakeFunctionResponse({"content": [{"type": "text", "text": "not json"}]})
        ]
        assert has_no_usable_market_data([event]) is True


class _FakeAccounts:
    def __init__(self):
        self.calls: list[dict] = []

    def record_usage(self, **kwargs):
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_pipeline_fails_fast_on_total_market_data_failure_without_billing():
    class NoDataRunner:
        session_service = FakeSessionService()

        async def run_async(self, **kwargs):
            yield _event_with_markets("analysis_event_retrieval", [], final=True)
            yield _event_with_markets("analysis_signal_retrieval", [], final=True)
            # Must never run — proves the short-circuit, not just a late failure.
            yield FakeEvent("analysis_news_retrieval", final=True)
            yield FakeEvent(
                "market_analyst_agent",
                report={"market_id": "", "primary_causal_driver": "UNKNOWN_ANOMALY"},
            )

    registry = AnalysisRegistry()
    record = registry.create("why did it move?", profile_id="user-1")
    accounts = _FakeAccounts()
    pipeline = AnalysisPipeline(registry, NoDataRunner(), accounts=accounts)
    await pipeline.run(record.analysis_id, "why did it move?", "some-slug", profile_id="user-1")

    result = registry.get(record.analysis_id)
    assert result.status is AnalysisStatus.FAILED
    assert result.error_type == "sagittarius-unavailable"
    assert result.report is None

    events = []
    while not record.queue.empty():
        events.append(record.queue.get_nowait())
    stage_starts = {e.stage for e in events if e is not None and e.event == "stage_started"}
    assert "news_retrieval" not in stage_starts
    assert "analysis" not in stage_starts
    error_events = [e for e in events if e is not None and e.event == "error"]
    assert error_events and error_events[0].data.get("error_type") == "sagittarius-unavailable"

    # F15's actual bug: an "I had no data" report was outcome="completed",
    # which monthly_usage counts. Pin the fix as a property of outcome
    # itself, not by re-deriving monthly_usage's filter here.
    assert accounts.calls, "usage must still be recorded for the attempt"
    assert accounts.calls[0]["outcome"] != "completed"


@pytest.mark.asyncio
async def test_pipeline_completes_normally_for_a_real_quiet_market():
    class QuietMarketRunner:
        session_service = FakeSessionService()

        async def run_async(self, **kwargs):
            yield _event_with_markets(
                "analysis_event_retrieval",
                [{"condition_id": "0xabc", "probability": 0.5}],
                final=True,
            )
            yield _event_with_markets(
                "analysis_signal_retrieval",
                [{"condition_id": "0xabc", "probability": 0.5, "volume_analysis": {}}],
                final=True,
            )
            yield FakeEvent("analysis_news_retrieval", final=True)
            yield FakeEvent(
                "market_analyst_agent",
                report={"market_id": "0xabc", "primary_causal_driver": "UNKNOWN_ANOMALY"},
            )

    registry = AnalysisRegistry()
    record = registry.create("why did it move?")
    await AnalysisPipeline(registry, QuietMarketRunner()).run(
        record.analysis_id, "why did it move?", "slug"
    )
    assert registry.get(record.analysis_id).status is AnalysisStatus.COMPLETED


@pytest.mark.asyncio
async def test_report_timestamp_is_overridden_to_server_time_not_the_models():
    """F16: an LLM cannot know the current time. The 09-17 production report
    was stamped 2024-06-18 — guaranteed wrong, not occasionally wrong."""

    class StaleTimestampRunner:
        session_service = FakeSessionService()

        async def run_async(self, **kwargs):
            yield FakeEvent(
                "market_analyst_agent",
                report={
                    "market_id": "0xabc",
                    "timestamp": "2024-06-18T12:00:00+00:00",
                },
            )

    registry = AnalysisRegistry()
    record = registry.create("why did it move?")
    before = datetime.now(UTC)
    await AnalysisPipeline(registry, StaleTimestampRunner()).run(
        record.analysis_id, "why did it move?", "slug"
    )
    after = datetime.now(UTC)

    report = registry.get(record.analysis_id).report
    stamped = datetime.fromisoformat(report["timestamp"])
    assert before <= stamped <= after
