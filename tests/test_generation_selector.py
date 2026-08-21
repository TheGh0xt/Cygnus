"""Which candidates become analyses, and why the rest do not.

Pure, so the rules are testable without a store, a clock or an MCP server —
the same reason `due_horizons` is pure in the evaluation worker.

Every rejection carries a reason. That is the direct lesson of 2026-08-20,
where a cycle reporting a bare count could not distinguish "nothing
qualified" from "the fetch is broken", and three documents guessed wrong
between them for five days.
"""

from datetime import UTC, datetime, timedelta

from src.generation.selector import (
    COOLDOWN_HOURS,
    Candidate,
    SkipReason,
    select_for_analysis,
)
from src.memory.store import RecentAnalysis

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _candidate(slug: str, change: float = 0.20, category: str = "Politics") -> Candidate:
    return Candidate(
        market_slug=slug,
        title=slug,
        category=category,
        probability=0.40,
        change_24h=change,
    )


class TestCooldown:
    def test_market_analysed_inside_the_window_is_skipped(self):
        recent = [
            RecentAnalysis("fed-decision", NOW - timedelta(hours=COOLDOWN_HOURS - 1))
        ]

        result = select_for_analysis(
            [_candidate("fed-decision")], recent, now=NOW, limit=2, daily_cap=8
        )

        assert result.selected == []
        assert result.skipped[0].market_slug == "fed-decision"
        assert result.skipped[0].reason is SkipReason.COOLDOWN

    def test_market_analysed_before_the_window_is_eligible_again(self):
        # Re-analysis is the point: 4.4 needs per-market history and 4.3 needs
        # a prior report to compare against. The cooldown only stops one
        # volatile market monopolising a single cycle.
        recent = [
            RecentAnalysis("fed-decision", NOW - timedelta(hours=COOLDOWN_HOURS + 1))
        ]

        result = select_for_analysis(
            [_candidate("fed-decision")], recent, now=NOW, limit=2, daily_cap=8
        )

        assert [c.market_slug for c in result.selected] == ["fed-decision"]

    def test_an_unanalysed_market_is_eligible(self):
        result = select_for_analysis(
            [_candidate("brand-new")], [], now=NOW, limit=2, daily_cap=8
        )
        assert [c.market_slug for c in result.selected] == ["brand-new"]


class TestDailyCap:
    def test_cap_counts_only_todays_analyses(self):
        recent = [
            RecentAnalysis("old", NOW - timedelta(days=3)),
            RecentAnalysis("today-1", NOW - timedelta(hours=5)),
        ]

        result = select_for_analysis(
            [_candidate("new")], recent, now=NOW, limit=2, daily_cap=2
        )

        # One analysis today, cap of 2, so one slot remains.
        assert [c.market_slug for c in result.selected] == ["new"]

    def test_reaching_the_cap_stops_the_cycle(self):
        recent = [
            RecentAnalysis(f"today-{i}", NOW - timedelta(hours=i + 1)) for i in range(8)
        ]

        result = select_for_analysis(
            [_candidate("new")], recent, now=NOW, limit=2, daily_cap=8
        )

        assert result.selected == []
        assert result.skipped[0].reason is SkipReason.DAILY_CAP_REACHED

    def test_cap_leaves_room_for_only_part_of_the_batch(self):
        recent = [
            RecentAnalysis(f"today-{i}", NOW - timedelta(hours=i + 1)) for i in range(7)
        ]

        result = select_for_analysis(
            [_candidate("a"), _candidate("b")], recent, now=NOW, limit=2, daily_cap=8
        )

        assert len(result.selected) == 1
        assert any(s.reason is SkipReason.DAILY_CAP_REACHED for s in result.skipped)

    def test_a_cap_of_zero_disables_generation_entirely(self):
        # The ceiling is a safety valve; setting it to zero must actually stop
        # the job rather than being read as "no limit".
        result = select_for_analysis(
            [_candidate("a")], [], now=NOW, limit=2, daily_cap=0
        )
        assert result.selected == []
        assert result.skipped[0].reason is SkipReason.DAILY_CAP_REACHED


class TestLimit:
    def test_takes_at_most_the_per_cycle_limit(self):
        candidates = [_candidate(f"m{i}") for i in range(5)]

        result = select_for_analysis(candidates, [], now=NOW, limit=2, daily_cap=8)

        assert len(result.selected) == 2

    def test_candidates_beyond_the_limit_are_not_reported_as_skips(self):
        # They were not rejected, only not reached this cycle. Recording them
        # as skips would bury the real reasons under routine noise.
        candidates = [_candidate(f"m{i}") for i in range(5)]

        result = select_for_analysis(candidates, [], now=NOW, limit=2, daily_cap=8)

        assert result.skipped == []


class TestOrdering:
    def test_preserves_the_order_discovery_supplied(self):
        # Sagittarius already round-robins across categories and ranks by
        # movement. Re-sorting here would undo that and let one category
        # dominate the corpus.
        candidates = [
            _candidate("pol", change=0.10, category="Politics"),
            _candidate("cry", change=0.90, category="Crypto"),
        ]

        result = select_for_analysis(candidates, [], now=NOW, limit=2, daily_cap=8)

        assert [c.market_slug for c in result.selected] == ["pol", "cry"]


class TestNoCandidates:
    def test_empty_discovery_is_reported_distinctly(self):
        result = select_for_analysis([], [], now=NOW, limit=2, daily_cap=8)

        assert result.selected == []
        assert result.no_candidates is True
