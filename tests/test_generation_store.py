"""What the generation cycle needs to know about what it has already done.

Two questions, one query. Before analysing a market the cycle must know when
that market was last analysed (the cooldown), and how many reports have been
written today (the daily spend ceiling). Both are answered from the same set
of recent rows, so the store exposes one read and the pure selector does the
arithmetic.
"""

from datetime import UTC, datetime, timedelta

from src.memory.store import SqliteMemoryStore
from src.schemas.report import MarketAnalysisReport

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _report() -> MarketAnalysisReport:
    return MarketAnalysisReport.model_validate(
        {
            "market_id": "0xabc",
            "timestamp": NOW.isoformat(),
            "summary": "Whale accumulation drove YES up 9 points.",
            "primary_causal_driver": "WHALE_ACTIVITY",
            "confidence_score": 0.85,
            "key_drivers": [],
        }
    )


def _store(tmp_path) -> SqliteMemoryStore:
    return SqliteMemoryStore(tmp_path / "gen.db")


class TestRecentAnalyses:
    def test_returns_slug_and_creation_time(self, tmp_path):
        store = _store(tmp_path)
        store.save_report(_report(), "fed-decision", 0.42, created_at=NOW)

        recent = store.recent_analyses(since=NOW - timedelta(hours=24))

        assert len(recent) == 1
        assert recent[0].market_slug == "fed-decision"
        assert recent[0].created_at == NOW

    def test_excludes_rows_older_than_the_window(self, tmp_path):
        store = _store(tmp_path)
        store.save_report(
            _report(), "ancient", 0.42, created_at=NOW - timedelta(days=9)
        )
        store.save_report(
            _report(), "recent", 0.42, created_at=NOW - timedelta(hours=2)
        )

        recent = store.recent_analyses(since=NOW - timedelta(hours=24))

        assert [r.market_slug for r in recent] == ["recent"]

    def test_returns_every_analysis_of_one_market_not_just_the_latest(self, tmp_path):
        # Re-analysis is deliberate — 4.4 needs per-market history — so the
        # cooldown must see each occurrence, not a deduplicated set.
        store = _store(tmp_path)
        store.save_report(
            _report(), "btc-150k", 0.42, created_at=NOW - timedelta(hours=20)
        )
        store.save_report(
            _report(), "btc-150k", 0.44, created_at=NOW - timedelta(hours=3)
        )

        recent = store.recent_analyses(since=NOW - timedelta(hours=24))

        assert len(recent) == 2

    def test_empty_store_returns_nothing(self, tmp_path):
        assert _store(tmp_path).recent_analyses(since=NOW - timedelta(hours=24)) == []

    def test_created_at_comes_back_timezone_aware(self, tmp_path):
        # A naive datetime here would silently compare wrong against the
        # tz-aware "now" the cycle uses, and the cooldown would misfire.
        store = _store(tmp_path)
        store.save_report(_report(), "fed-decision", 0.42, created_at=NOW)

        recent = store.recent_analyses(since=NOW - timedelta(hours=24))

        assert recent[0].created_at.tzinfo is not None


class TestRecentAnalysesOnPostgres:
    """The deployed store is Postgres, so the same contract must hold there."""

    def test_filters_on_created_at_and_selects_only_what_it_needs(self):
        from tests.test_postgres_store import RecordingStore

        store = RecordingStore(response=[])
        store.recent_analyses(since=NOW - timedelta(hours=24))

        call = store.calls[-1]
        assert call["method"] == "GET"
        params = call["params"]
        assert params["created_at"] == f"gte.{(NOW - timedelta(hours=24)).isoformat()}"
        # Pulling report_json here would drag every stored report across the
        # wire to answer a question about slugs and timestamps.
        assert "report_json" not in params["select"]

    def test_decodes_rows_into_recent_analyses(self):
        from tests.test_postgres_store import RecordingStore

        # RecordingStore pops one payload per request, so the row list is
        # wrapped in an outer list representing a single response.
        store = RecordingStore(
            response=[
                [
                    {"market_slug": "fed-decision", "created_at": NOW.isoformat()},
                    {"market_slug": "btc-150k", "created_at": NOW.isoformat()},
                ]
            ]
        )

        recent = store.recent_analyses(since=NOW - timedelta(hours=24))

        assert [r.market_slug for r in recent] == ["fed-decision", "btc-150k"]
        assert recent[0].created_at.tzinfo is not None
