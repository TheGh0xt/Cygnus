from datetime import UTC, datetime

from src.agents.callbacks import make_persist_report_callback, resolve_slug_from_state
from src.schemas.report import MarketAnalysisReport


class FakeStore:
    def __init__(self):
        self.saved = []

    def save_report(self, report, market_slug, price_at_report, created_at=None):
        self.saved.append((report, market_slug, price_at_report))
        return len(self.saved)


class FakeFetcher:
    def __init__(self, price):
        self.price = price

    def current_probability(self, market_slug):
        return self.price


class FakeContext:
    def __init__(self, state):
        self.state = state
        self.agent_name = "market_analyst_agent"


def _report():
    return MarketAnalysisReport(
        market_id="0xabc",
        timestamp=datetime.now(tz=UTC),
        summary="whale bought",
        primary_causal_driver="WHALE_ACTIVITY",
        confidence_score=0.75,
        key_drivers=[],
    )


def test_resolve_slug_prefers_explicit_state_key():
    assert resolve_slug_from_state({"event_slug": "world-cup-winner"}) == (
        "world-cup-winner"
    )


def test_resolve_slug_returns_none_when_absent():
    assert resolve_slug_from_state({}) is None


def test_callback_persists_report_with_observed_price():
    store, fetcher = FakeStore(), FakeFetcher(0.62)
    cb = make_persist_report_callback(store, fetcher)
    state = {
        "event_slug": "world-cup-winner",
        "market_analysis_report": _report().model_dump(),
    }
    cb(callback_context=FakeContext(state))
    assert len(store.saved) == 1
    saved_report, slug, price = store.saved[0]
    assert slug == "world-cup-winner"
    assert price == 0.62
    assert saved_report.market_id == "0xabc"


def test_callback_accepts_adks_keyword_invocation():
    """ADK calls after-agent callbacks as `callback(callback_context=...)`.

    Pinned because a positional-only test passes happily while the real run
    dies with "unexpected keyword argument" — which is exactly how this bug
    reached a live analysis.
    """
    import inspect

    cb = make_persist_report_callback(FakeStore(), FakeFetcher(0.5))
    assert list(inspect.signature(cb).parameters) == ["callback_context"]


def test_callback_is_noop_when_report_missing():
    store, fetcher = FakeStore(), FakeFetcher(0.62)
    cb = make_persist_report_callback(store, fetcher)
    cb(callback_context=FakeContext({"event_slug": "x"}))
    assert store.saved == []


def test_callback_survives_price_fetch_failure():
    # Degraded-but-successful: a missing price must not lose the report.
    class Failing:
        def current_probability(self, market_slug):
            raise RuntimeError("sagittarius down")

    store = FakeStore()
    cb = make_persist_report_callback(store, Failing())
    state = {"event_slug": "x", "market_analysis_report": _report().model_dump()}
    cb(callback_context=FakeContext(state))
    assert len(store.saved) == 1
    assert store.saved[0][2] is None


async def test_price_is_fetched_even_inside_a_running_event_loop():
    """The API calls this callback from inside asyncio.

    SagittariusPriceFetcher uses asyncio.run(), which raises inside a running
    loop. A direct call therefore fails in production while every unit test
    that runs synchronously passes — so every report was persisted with a null
    price and was silently unscoreable. The fetch runs in a worker thread to
    avoid that.
    """
    import asyncio

    class LoopSensitiveFetcher:
        def current_probability(self, market_slug):
            # Mirrors the real fetcher: blows up if a loop is already running
            # on this thread.
            return asyncio.run(self._value())

        async def _value(self):
            return 0.33

    store = FakeStore()
    cb = make_persist_report_callback(store, LoopSensitiveFetcher())
    state = {
        "event_slug": "world-cup-winner",
        "market_analysis_report": _report().model_dump(),
    }

    cb(callback_context=FakeContext(state))

    assert len(store.saved) == 1
    assert store.saved[0][2] == 0.33, "price must survive the running loop"


def test_a_store_failure_is_logged_as_lost_data(caplog):
    """A completed report that cannot be stored is unrecoverable.

    The price observed at this moment is the baseline the evaluation worker
    scores against in 48 hours and cannot be reconstructed afterwards, so this
    must be impossible to miss in a log.
    """

    class BrokenStore:
        def save_report(self, *args, **kwargs):
            raise RuntimeError("row-level security policy")

    cb = make_persist_report_callback(BrokenStore(), FakeFetcher(0.5))
    state = {
        "event_slug": "world-cup-winner",
        "market_analysis_report": _report().model_dump(),
    }

    with caplog.at_level("CRITICAL"):
        cb(callback_context=FakeContext(state))

    assert "REPORT LOST" in caplog.text
    assert "world-cup-winner" in caplog.text


def test_a_store_failure_does_not_fail_the_analysis():
    # The user still gets their report. Raising here would lose the analysis
    # as well as the row, which helps nobody.
    class BrokenStore:
        def save_report(self, *args, **kwargs):
            raise RuntimeError("store down")

    cb = make_persist_report_callback(BrokenStore(), FakeFetcher(0.5))
    state = {
        "event_slug": "x",
        "market_analysis_report": _report().model_dump(),
    }
    assert cb(callback_context=FakeContext(state)) is None
