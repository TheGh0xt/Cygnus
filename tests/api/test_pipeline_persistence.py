"""A completed analysis must persist its report.

This is the assertion that was missing. Persistence used to live in the
analyst's after_agent_callback, and the tests for it called that callback
directly — so they passed while the real path never fired, because
CallbackContext.state does not contain the analyst's output_key write at the
time the callback runs.

These drive the pipeline instead, which is what production does.
"""

import pytest

from src.api.persistence import ReportPersistence
from src.api.pipeline import AnalysisPipeline
from src.api.registry import AnalysisRegistry, AnalysisStatus

REPORT = {
    "market_id": "0xabc",
    "timestamp": "2026-08-14T00:00:00+00:00",
    "summary": "whale accumulation",
    "primary_causal_driver": "WHALE_ACTIVITY",
    "confidence_score": 0.75,
    "key_drivers": [],
}


class FakeActions:
    def __init__(self, state_delta=None):
        self.state_delta = state_delta or {}


class FakeEvent:
    def __init__(self, author, report=None):
        self.author = author
        self.actions = FakeActions({"market_analysis_report": report} if report else {})
        self._final = report is not None

    def is_final_response(self):
        return self._final


class FakeSessionService:
    @staticmethod
    async def create_session(**kwargs):
        class S:
            id = kwargs.get("session_id", "s1")
            # Deliberately empty: this mirrors ADK, where the analyst's
            # output_key write is NOT in session state at this point.
            state: dict = {}

        return S()


class FakeRunner:
    session_service = FakeSessionService()

    def __init__(self, report=REPORT):
        self._report = report

    async def run_async(self, **kwargs):
        yield FakeEvent("analysis_event_retrieval")
        yield FakeEvent("market_analyst_agent", report=self._report)


class RecordingStore:
    def __init__(self, error: Exception | None = None):
        self.saved: list[tuple] = []
        self.error = error

    def save_report(self, report, slug, price, created_at=None):
        if self.error:
            raise self.error
        self.saved.append((report, slug, price))
        return len(self.saved)


class FakeFetcher:
    def __init__(self, price=0.61):
        self.price = price

    def current_probability(self, slug):
        return self.price


def _pipeline(store, fetcher=None, runner=None):
    registry = AnalysisRegistry()
    pipeline = AnalysisPipeline(
        registry,
        runner or FakeRunner(),
        persistence=ReportPersistence(store, fetcher or FakeFetcher()),
    )
    return registry, pipeline


@pytest.mark.asyncio
async def test_completed_analysis_is_persisted():
    store = RecordingStore()
    registry, pipeline = _pipeline(store)
    record = registry.create("q")

    await pipeline.run(record.analysis_id, "q", "world-cup-winner")

    assert registry.get(record.analysis_id).status is AnalysisStatus.COMPLETED
    assert len(store.saved) == 1, "a completed analysis must store its report"
    report, slug, price = store.saved[0]
    assert slug == "world-cup-winner"
    assert price == 0.61
    assert report.primary_causal_driver.value == "WHALE_ACTIVITY"


@pytest.mark.asyncio
async def test_price_is_captured_at_report_time():
    # The stored price is the baseline the evaluation worker scores against,
    # and it cannot be recovered later.
    store = RecordingStore()
    registry, pipeline = _pipeline(store, fetcher=FakeFetcher(price=0.42))
    record = registry.create("q")

    await pipeline.run(record.analysis_id, "q", "slug")

    assert store.saved[0][2] == 0.42


@pytest.mark.asyncio
async def test_price_failure_still_stores_the_report():
    class Failing:
        def current_probability(self, slug):
            raise RuntimeError("sagittarius unreachable")

    store = RecordingStore()
    registry, pipeline = _pipeline(store, fetcher=Failing())
    record = registry.create("q")

    await pipeline.run(record.analysis_id, "q", "slug")

    assert len(store.saved) == 1
    assert store.saved[0][2] is None


@pytest.mark.asyncio
async def test_store_failure_is_logged_but_does_not_fail_the_analysis(caplog):
    store = RecordingStore(error=RuntimeError("row-level security policy"))
    registry, pipeline = _pipeline(store)
    record = registry.create("q")

    with caplog.at_level("CRITICAL"):
        await pipeline.run(record.analysis_id, "q", "world-cup-winner")

    # The user still gets their report.
    assert registry.get(record.analysis_id).status is AnalysisStatus.COMPLETED
    # But the loss is impossible to miss.
    assert "REPORT LOST" in caplog.text
    assert "world-cup-winner" in caplog.text


@pytest.mark.asyncio
async def test_failed_analysis_persists_nothing():
    class Boom:
        session_service = FakeSessionService()

        async def run_async(self, **kwargs):
            raise RuntimeError("model unavailable")
            yield

    store = RecordingStore()
    registry, pipeline = _pipeline(store, runner=Boom())
    record = registry.create("q")

    await pipeline.run(record.analysis_id, "q", "slug")

    assert registry.get(record.analysis_id).status is AnalysisStatus.FAILED
    assert store.saved == []
