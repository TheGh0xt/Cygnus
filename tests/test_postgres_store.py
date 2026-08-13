"""PostgresMemoryStore: the parts testable without a database.

The real integration is verified separately against live Postgres (see
scripts/verify_postgres_store.py) because mocks cannot tell you whether
jsonb, timestamptz and PostgREST behave the way this code assumes. What is
covered here is the contract: request shapes, type handling and the store
selection rule.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.memory import build_memory_store
from src.memory.postgres_store import MemoryStoreError, PostgresMemoryStore, _parse_ts
from src.memory.store import SqliteMemoryStore
from src.schemas.report import MarketAnalysisReport


def _report(confidence: float = 0.8) -> MarketAnalysisReport:
    return MarketAnalysisReport(
        market_id="0xabc",
        timestamp=datetime.now(tz=UTC),
        summary="whale bought",
        primary_causal_driver="WHALE_ACTIVITY",
        confidence_score=confidence,
        key_drivers=[],
    )


class RecordingStore(PostgresMemoryStore):
    """Captures requests instead of sending them."""

    def __init__(self, response=None):
        super().__init__(base_url="https://example.test", service_key="k")
        self.calls = []
        self._response = response if response is not None else []

    def _request(self, method, path, **kwargs):
        self.calls.append({"method": method, "path": path, **kwargs})

        class R:
            @staticmethod
            def json():
                return RecordingStore._pop(self)

        return R()

    @staticmethod
    def _pop(store):
        if isinstance(store._response, list) and store._response:
            return store._response.pop(0)
        return store._response if not isinstance(store._response, list) else []


class TestConstruction:
    def test_requires_credentials(self):
        with pytest.raises(MemoryStoreError, match="SUPABASE_URL"):
            PostgresMemoryStore(base_url="", service_key="")


class TestSaveReport:
    def test_sends_json_object_not_a_json_string(self):
        # The column is jsonb. Sending model_dump_json() would store a quoted
        # string containing JSON, which every reader then parses twice and
        # which breaks jsonb path queries.
        store = RecordingStore(response=[[{"id": 7}]])
        store.save_report(_report(), "slug", 0.58)
        payload = store.calls[0]["json"]
        assert isinstance(payload["report_json"], dict)
        assert payload["report_json"]["market_id"] == "0xabc"

    def test_returns_the_new_id(self):
        store = RecordingStore(response=[[{"id": 42}]])
        assert store.save_report(_report(), "slug", 0.58) == 42

    def test_null_price_is_sent_through(self):
        store = RecordingStore(response=[[{"id": 1}]])
        store.save_report(_report(), "slug", None)
        assert store.calls[0]["json"]["price_at_report"] is None

    def test_asks_postgrest_to_return_the_row(self):
        # Without this header PostgREST returns an empty body and there is no
        # id to hand back.
        store = RecordingStore(response=[[{"id": 1}]])
        store.save_report(_report(), "slug", 0.5)
        assert "return=representation" in store.calls[0]["headers"]["prefer"]


class TestDueForEvaluation:
    def test_filters_on_unevaluated_and_age(self):
        store = RecordingStore(response=[[]])
        now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
        store.get_reports_due_for_evaluation(now, min_age_hours=48)
        params = store.calls[0]["params"]
        assert params["evaluated_at"] == "is.null"
        assert params["created_at"] == f"lte.{(now - timedelta(hours=48)).isoformat()}"


class TestRecordEvaluation:
    def test_missing_id_raises_keyerror(self):
        # Same contract as the SQLite store: a missing id is a caller bug.
        store = RecordingStore(response=[[]])
        with pytest.raises(KeyError):
            store.record_evaluation(1, 0.9, "CONFIRMED", datetime.now(tz=UTC))

    def test_updates_both_the_column_and_the_embedded_report(self):
        # The score is denormalised, so leaving the embedded copy stale would
        # make a report disagree with its own row.
        store = RecordingStore(
            response=[[{"report_json": _report(0.8).model_dump(mode="json")}]]
        )
        store.record_evaluation(5, 0.85, "CONFIRMED", datetime.now(tz=UTC))
        patch = store.calls[-1]["json"]
        assert patch["confidence_score"] == 0.85
        assert patch["report_json"]["confidence_score"] == 0.85
        assert patch["outcome"] == "CONFIRMED"


class TestTimestampParsing:
    def test_handles_full_offset(self):
        assert _parse_ts("2026-08-13T12:00:00+00:00").tzinfo is not None

    def test_handles_postgres_short_offset(self):
        # Postgres can render UTC as '+00', which fromisoformat rejects.
        assert _parse_ts("2026-08-13T12:00:00+00").tzinfo is not None


class TestStoreSelection:
    def test_sqlite_when_supabase_is_not_configured(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        store = build_memory_store(str(tmp_path / "local.db"))
        assert isinstance(store, SqliteMemoryStore)

    def test_postgres_when_supabase_is_configured(self, tmp_path, monkeypatch):
        # Selection is by configuration, not a flag: a deployed instance must
        # not be able to silently fall back to a disk that vanishes on restart.
        monkeypatch.setenv("SUPABASE_URL", "https://example.test")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
        store = build_memory_store(str(tmp_path / "unused.db"))
        assert isinstance(store, PostgresMemoryStore)

    def test_partial_configuration_falls_back_to_sqlite(self, tmp_path, monkeypatch):
        # A URL with no key cannot authenticate; failing over to SQLite beats
        # crashing at startup.
        monkeypatch.setenv("SUPABASE_URL", "https://example.test")
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        assert isinstance(
            build_memory_store(str(tmp_path / "local.db")), SqliteMemoryStore
        )
