import pytest

from src.api.pipeline import AnalysisPipeline, stage_for_author
from src.api.registry import AnalysisRegistry, AnalysisStatus


def test_known_authors_map_to_public_stage_names():
    assert stage_for_author("analysis_event_retrieval") == "event_retrieval"
    assert stage_for_author("analysis_signal_retrieval") == "signal_retrieval"
    assert stage_for_author("analysis_news_retrieval") == "news_retrieval"
    assert stage_for_author("market_analyst_agent") == "analysis"


def test_unknown_author_has_no_public_stage():
    # Internal agents must not leak into the public event stream.
    assert stage_for_author("polymarket_orchestrator") is None


class FakeEvent:
    def __init__(self, author, report=None):
        self.author = author
        self.report = report

    def is_final_response(self):
        return self.report is not None


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
