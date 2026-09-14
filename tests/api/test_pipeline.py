import logging

import pytest

from src.api.pipeline import (
    AnalysisPipeline,
    _citation_coverage_violations,
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


class FakeEvent:
    def __init__(self, author, report=None, final=False):
        self.author = author
        self.actions = FakeActions({"market_analysis_report": report} if report else {})
        self._final = final or report is not None

    def is_final_response(self):
        return self._final


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
