"""Escalating evaluation horizons.

A single check at T+48h answers "was it right" and nothing else. Checking at
12, 18, 24 and 48 hours answers how durable the explanation was, and
accumulates data roughly four times faster.

The trap these tests exist to prevent: the +0.05 / -0.10 matrix was designed
to apply once. Running it at four checkpoints would swing confidence four
times as far and let a wobbling report whipsaw its own score. So only the
canonical 48h checkpoint adjusts confidence; the rest are observations.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.evaluation.worker import (
    CANONICAL_HORIZON,
    EVALUATION_HORIZONS,
    due_horizons,
    run_evaluation_cycle,
)
from src.memory.store import SqliteMemoryStore
from src.schemas.report import MarketAnalysisReport

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def make_report(confidence: float = 0.80) -> MarketAnalysisReport:
    return MarketAnalysisReport(
        market_id="0xabc",
        timestamp=NOW,
        summary="whale accumulation",
        primary_causal_driver="WHALE_ACTIVITY",
        confidence_score=confidence,
        key_drivers=[],
    )


class FakeFetcher:
    def __init__(self, prices: dict[str, float | None]):
        self.prices = prices
        self.calls = 0

    def current_probability(self, slug: str) -> float | None:
        self.calls += 1
        return self.prices.get(slug)


@pytest.fixture
def store(tmp_path):
    return SqliteMemoryStore(tmp_path / "horizons.db")


class TestDueHorizons:
    def test_nothing_is_due_immediately(self):
        assert due_horizons(NOW, NOW, set()) == []

    def test_only_elapsed_horizons_are_due(self):
        assert due_horizons(NOW, NOW + timedelta(hours=13), set()) == [12]
        assert due_horizons(NOW, NOW + timedelta(hours=19), set()) == [12, 18]
        assert due_horizons(NOW, NOW + timedelta(hours=25), set()) == [12, 18, 24]

    def test_all_horizons_due_after_the_last_one(self):
        assert due_horizons(NOW, NOW + timedelta(hours=49), set()) == list(
            EVALUATION_HORIZONS
        )

    def test_already_recorded_horizons_are_skipped(self):
        # This is what makes a cycle idempotent and lets it run every six
        # hours without rescoring anything.
        assert due_horizons(NOW, NOW + timedelta(hours=49), {12, 18}) == [24, 48]

    def test_exactly_on_the_boundary_counts_as_due(self):
        assert due_horizons(NOW, NOW + timedelta(hours=12), set()) == [12]


class TestConfidenceMovesOnce:
    def test_early_checkpoints_do_not_touch_confidence(self, store):
        report_id = store.save_report(make_report(0.80), "slug-a", 0.58, created_at=NOW)

        # 25 hours: the 12, 18 and 24 hour checkpoints are due, 48 is not.
        run_evaluation_cycle(
            store, FakeFetcher({"slug-a": 0.70}), now=NOW + timedelta(hours=25)
        )

        stored = store.get_history_for_market("0xabc")[0]
        assert stored.report.confidence_score == pytest.approx(0.80), (
            "confidence must not move before the canonical horizon"
        )
        assert stored.outcome is None
        assert len(store.get_checkpoints(report_id)) == 3

    def test_canonical_checkpoint_moves_it_exactly_once(self, store):
        store.save_report(make_report(0.80), "slug-a", 0.58, created_at=NOW)

        run_evaluation_cycle(
            store, FakeFetcher({"slug-a": 0.70}), now=NOW + timedelta(hours=49)
        )

        stored = store.get_history_for_market("0xabc")[0]
        # One application of +0.05, not four.
        assert stored.report.confidence_score == pytest.approx(0.85)
        assert stored.outcome == "CONFIRMED"

    def test_a_reversal_also_moves_once(self, store):
        store.save_report(make_report(0.80), "slug-a", 0.58, created_at=NOW)

        run_evaluation_cycle(
            store, FakeFetcher({"slug-a": 0.30}), now=NOW + timedelta(hours=49)
        )

        stored = store.get_history_for_market("0xabc")[0]
        # -0.10 once, not -0.40.
        assert stored.report.confidence_score == pytest.approx(0.70)
        assert stored.outcome == "REVERSED"


class TestIdempotence:
    def test_rerunning_scores_nothing_new(self, store):
        store.save_report(make_report(0.80), "slug-a", 0.58, created_at=NOW)
        later = NOW + timedelta(hours=49)
        fetcher = FakeFetcher({"slug-a": 0.70})

        first = run_evaluation_cycle(store, fetcher, now=later)
        second = run_evaluation_cycle(store, fetcher, now=later)

        assert first.scored == 4
        assert second.scored == 0, "a re-run must not rescore recorded checkpoints"

    def test_confidence_is_not_moved_twice_by_a_rerun(self, store):
        store.save_report(make_report(0.80), "slug-a", 0.58, created_at=NOW)
        later = NOW + timedelta(hours=49)
        fetcher = FakeFetcher({"slug-a": 0.70})

        run_evaluation_cycle(store, fetcher, now=later)
        run_evaluation_cycle(store, fetcher, now=later)

        stored = store.get_history_for_market("0xabc")[0]
        assert stored.report.confidence_score == pytest.approx(0.85)


class TestProgressiveScoring:
    def test_checkpoints_accumulate_across_cycles(self, store):
        report_id = store.save_report(make_report(0.80), "slug-a", 0.58, created_at=NOW)
        fetcher = FakeFetcher({"slug-a": 0.70})

        run_evaluation_cycle(store, fetcher, now=NOW + timedelta(hours=13))
        assert store.get_recorded_horizons(report_id) == {12}

        run_evaluation_cycle(store, fetcher, now=NOW + timedelta(hours=25))
        assert store.get_recorded_horizons(report_id) == {12, 18, 24}

        run_evaluation_cycle(store, fetcher, now=NOW + timedelta(hours=49))
        assert store.get_recorded_horizons(report_id) == set(EVALUATION_HORIZONS)

    def test_a_checkpoint_records_the_price_it_saw(self, store):
        # Kept so a scoring decision can be re-examined without refetching
        # history that may no longer be available.
        report_id = store.save_report(make_report(0.80), "slug-a", 0.58, created_at=NOW)
        run_evaluation_cycle(
            store, FakeFetcher({"slug-a": 0.71}), now=NOW + timedelta(hours=13)
        )

        checkpoint = store.get_checkpoints(report_id)[0]
        assert checkpoint.horizon_hours == 12
        assert checkpoint.observed_price == pytest.approx(0.71)
        assert checkpoint.is_canonical is False

    def test_durability_is_visible_when_a_report_reverses_late(self, store):
        # The whole point: "held early, reversed late" must be distinguishable
        # from "wrong from the start".
        report_id = store.save_report(make_report(0.80), "slug-a", 0.58, created_at=NOW)

        run_evaluation_cycle(
            store, FakeFetcher({"slug-a": 0.70}), now=NOW + timedelta(hours=25)
        )
        run_evaluation_cycle(
            store, FakeFetcher({"slug-a": 0.20}), now=NOW + timedelta(hours=49)
        )

        outcomes = {
            c.horizon_hours: c.outcome for c in store.get_checkpoints(report_id)
        }
        assert outcomes[12] == "CONFIRMED"
        assert outcomes[24] == "CONFIRMED"
        assert outcomes[CANONICAL_HORIZON] == "REVERSED"


class TestCostControl:
    def test_price_is_fetched_once_per_report_per_cycle(self, store):
        # Four due horizons must not mean four Sagittarius calls; they all
        # score against the same observation.
        store.save_report(make_report(0.80), "slug-a", 0.58, created_at=NOW)
        fetcher = FakeFetcher({"slug-a": 0.70})

        run_evaluation_cycle(store, fetcher, now=NOW + timedelta(hours=49))

        assert fetcher.calls == 1

    def test_unfetchable_price_leaves_everything_due(self, store):
        report_id = store.save_report(make_report(0.80), "slug-a", 0.58, created_at=NOW)

        count = run_evaluation_cycle(
            store, FakeFetcher({}), now=NOW + timedelta(hours=49)
        )

        assert count.scored == 0
        assert store.get_recorded_horizons(report_id) == set()
