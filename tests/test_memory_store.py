from datetime import UTC, datetime, timedelta

import pytest

from src.memory.store import SqliteMemoryStore
from src.schemas.report import MarketAnalysisReport

NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)


def make_report(
    market_id: str = "0xabc", confidence: float = 0.85
) -> MarketAnalysisReport:
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


@pytest.fixture
def store(tmp_path):
    return SqliteMemoryStore(tmp_path / "memory.db")


def test_save_and_history_round_trip(store):
    report_id = store.save_report(
        make_report(), "will-btc-hit-150k", 0.58, created_at=NOW
    )
    assert report_id > 0

    history = store.get_history_for_market("0xabc")
    assert len(history) == 1
    stored = history[0]
    assert stored.id == report_id
    assert stored.market_slug == "will-btc-hit-150k"
    assert stored.price_at_report == 0.58
    assert stored.report == make_report()
    assert stored.evaluated_at is None
    assert stored.outcome is None


def test_young_report_is_not_due(store):
    store.save_report(make_report(), "slug", 0.58, created_at=NOW)
    due = store.get_reports_due_for_evaluation(NOW + timedelta(hours=47))
    assert due == []


def test_old_report_is_due(store):
    store.save_report(make_report(), "slug", 0.58, created_at=NOW)
    due = store.get_reports_due_for_evaluation(NOW + timedelta(hours=49))
    assert len(due) == 1


def test_record_evaluation_updates_and_removes_from_due(store):
    report_id = store.save_report(
        make_report(confidence=0.85), "slug", 0.58, created_at=NOW
    )
    later = NOW + timedelta(hours=49)

    store.record_evaluation(
        report_id, new_confidence=0.9, outcome="CONFIRMED", evaluated_at=later
    )

    assert store.get_reports_due_for_evaluation(later) == []
    stored = store.get_history_for_market("0xabc")[0]
    assert stored.outcome == "CONFIRMED"
    assert stored.evaluated_at == later
    assert stored.report.confidence_score == 0.9


def test_history_is_per_market_and_oldest_first(store):
    store.save_report(make_report("0xaaa"), "a", 0.5, created_at=NOW)
    store.save_report(
        make_report("0xbbb"), "b", 0.5, created_at=NOW + timedelta(hours=1)
    )
    store.save_report(
        make_report("0xaaa"), "a", 0.6, created_at=NOW + timedelta(hours=2)
    )

    history = store.get_history_for_market("0xaaa")
    assert [s.price_at_report for s in history] == [0.5, 0.6]
    assert all(s.report.market_id == "0xaaa" for s in history)


def test_store_is_usable_from_another_thread(tmp_path):
    """The API opens the store at startup and uses it from request handlers.

    Those run on different threads, and sqlite3 rejects cross-thread use by
    default. That surfaced as a 500 on the evaluation endpoint with
    "SQLite objects created in a thread can only be used in that same thread",
    which would have broken every local run while production (Postgres) looked
    fine.
    """
    import threading

    store = SqliteMemoryStore(tmp_path / "threaded.db")
    store.save_report(make_report(), "slug", 0.5, created_at=NOW)

    results: list[object] = []

    def read_from_another_thread():
        try:
            results.append(len(store.get_history_for_market("0xabc")))
        except Exception as exc:  # noqa: BLE001 — the assertion is the point
            results.append(exc)

    thread = threading.Thread(target=read_from_another_thread)
    thread.start()
    thread.join()

    assert results == [1], f"cross-thread read failed: {results}"
