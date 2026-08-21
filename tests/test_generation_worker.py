"""The generation cycle end to end, with everything faked but the logic.

The cycle returns a structured report rather than a count. That is the whole
lesson of 2026-08-20 applied up front: `{"evaluated": 0}` could mean "nothing
was due", "no baseline price" or "the fetch is broken", and for five days the
project's own status doc asserted the wrong one of the three.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.generation.discovery import DiscoveryError
from src.generation.selector import Candidate, SkipReason
from src.generation.worker import run_generation_cycle
from src.memory.store import RecentAnalysis

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _candidate(slug: str, category: str = "Politics") -> Candidate:
    return Candidate(
        market_slug=slug,
        title=f"Title for {slug}",
        category=category,
        probability=0.40,
        change_24h=0.25,
    )


class FakeDiscovery:
    def __init__(self, candidates=None, error: Exception | None = None):
        self._candidates = candidates or []
        self._error = error
        self.calls = []

    async def moving_markets(self, categories=None, limit=20):
        self.calls.append({"categories": categories, "limit": limit})
        if self._error:
            raise self._error
        return self._candidates


class FakeStore:
    def __init__(self, recent=None):
        self._recent = recent or []
        self.since_asked = None

    def recent_analyses(self, since):
        self.since_asked = since
        return self._recent


class FakePipeline:
    def __init__(self, fail_on: set[str] | None = None):
        self.runs = []
        self._fail_on = fail_on or set()

    async def run(self, analysis_id, query, slug, profile_id=None):
        self.runs.append({"id": analysis_id, "query": query, "slug": slug})
        if slug in self._fail_on:
            raise RuntimeError(f"pipeline exploded on {slug}")


class FakeRegistry:
    def __init__(self):
        self.created = []

    def create(self, query):
        from types import SimpleNamespace

        self.created.append(query)
        return SimpleNamespace(analysis_id=f"gen-{len(self.created)}")


async def _run(**kw):
    defaults = dict(
        store=FakeStore(),
        discovery=FakeDiscovery(),
        pipeline=FakePipeline(),
        registry=FakeRegistry(),
        now=NOW,
        limit=2,
        daily_cap=8,
    )
    defaults.update(kw)
    return await run_generation_cycle(**defaults)


class TestGenerating:
    @pytest.mark.asyncio
    async def test_runs_the_pipeline_once_per_selected_market(self):
        pipeline = FakePipeline()
        report = await _run(
            discovery=FakeDiscovery([_candidate("a"), _candidate("b")]),
            pipeline=pipeline,
        )

        assert report.generated == 2
        assert [r["slug"] for r in pipeline.runs] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_query_names_the_market_and_asks_why_it_moved(self):
        # The pipeline routes on intent, so a generated query has to read like
        # a causal question or it will not reach the analysis pipeline.
        pipeline = FakePipeline()
        await _run(discovery=FakeDiscovery([_candidate("fed-decision")]), pipeline=pipeline)

        query = pipeline.runs[0]["query"].lower()
        assert "why" in query
        assert "fed-decision" in query or "title for fed-decision" in query

    @pytest.mark.asyncio
    async def test_respects_the_per_cycle_limit(self):
        pipeline = FakePipeline()
        await _run(
            discovery=FakeDiscovery([_candidate(f"m{i}") for i in range(5)]),
            pipeline=pipeline,
            limit=2,
        )
        assert len(pipeline.runs) == 2


class TestFailuresAreReportedNotSwallowed:
    @pytest.mark.asyncio
    async def test_discovery_failure_is_reported_not_raised(self):
        report = await _run(
            discovery=FakeDiscovery(error=DiscoveryError("sagittarius asleep"))
        )

        assert report.generated == 0
        assert report.discovery_error is not None
        assert "asleep" in report.discovery_error

    @pytest.mark.asyncio
    async def test_one_failing_market_does_not_stop_the_others(self):
        pipeline = FakePipeline(fail_on={"a"})
        report = await _run(
            discovery=FakeDiscovery([_candidate("a"), _candidate("b")]),
            pipeline=pipeline,
        )

        assert report.generated == 1
        assert any(
            s.reason is SkipReason.PIPELINE_FAILED and s.market_slug == "a"
            for s in report.skipped
        )

    @pytest.mark.asyncio
    async def test_an_empty_feed_is_distinguishable_from_everything_skipped(self):
        quiet = await _run(discovery=FakeDiscovery([]))
        assert quiet.no_candidates is True

        blocked = await _run(
            discovery=FakeDiscovery([_candidate("a")]),
            store=FakeStore([RecentAnalysis("a", NOW - timedelta(hours=1))]),
        )
        assert blocked.no_candidates is False
        assert blocked.skipped[0].reason is SkipReason.COOLDOWN


class TestGuardrails:
    @pytest.mark.asyncio
    async def test_cooldown_is_enforced_against_the_store(self):
        pipeline = FakePipeline()
        report = await _run(
            discovery=FakeDiscovery([_candidate("a")]),
            store=FakeStore([RecentAnalysis("a", NOW - timedelta(hours=2))]),
            pipeline=pipeline,
        )

        assert pipeline.runs == []
        assert report.skipped[0].reason is SkipReason.COOLDOWN

    @pytest.mark.asyncio
    async def test_daily_cap_stops_generation(self):
        pipeline = FakePipeline()
        recent = [
            RecentAnalysis(f"t{i}", NOW - timedelta(hours=i + 1)) for i in range(8)
        ]
        report = await _run(
            discovery=FakeDiscovery([_candidate("a")]),
            store=FakeStore(recent),
            pipeline=pipeline,
            daily_cap=8,
        )

        assert pipeline.runs == []
        assert report.skipped[0].reason is SkipReason.DAILY_CAP_REACHED

    @pytest.mark.asyncio
    async def test_store_is_asked_for_a_window_covering_both_rules(self):
        # The cooldown needs 12h; the daily cap needs back to midnight.
        # Asking for too short a window would silently under-count the cap.
        store = FakeStore()
        await _run(discovery=FakeDiscovery([_candidate("a")]), store=store)

        assert store.since_asked <= NOW.replace(hour=0, minute=0, second=0, microsecond=0)
