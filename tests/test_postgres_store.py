"""PostgresMemoryStore: the parts testable without a database.

The real integration is verified separately against live Postgres (see
scripts/verify_postgres_store.py) because mocks cannot tell you whether
jsonb, timestamptz and PostgREST behave the way this code assumes. What is
covered here is the contract: request shapes, type handling and the store
selection rule.
"""

from datetime import UTC, datetime, timedelta

import httpx
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

    def test_sends_stated_confidence_alongside_confidence(self):
        # B.11: the two start equal, but only confidence_score moves later —
        # stated_confidence_score is the fixed baseline calibration bins on.
        store = RecordingStore(response=[[{"id": 1}]])
        store.save_report(_report(confidence=0.72), "slug", 0.5)
        payload = store.calls[0]["json"]
        assert payload["stated_confidence_score"] == 0.72
        assert payload["confidence_score"] == 0.72


class TestDueForEvaluation:
    def test_filters_on_unevaluated_and_age(self):
        store = RecordingStore(response=[[]])
        now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
        store.get_reports_due_for_evaluation(now, min_age_hours=48)
        params = store.calls[0]["params"]
        assert params["evaluated_at"] == "is.null"
        assert params["created_at"] == f"lte.{(now - timedelta(hours=48)).isoformat()}"


def _row(**overrides) -> dict:
    row = {
        "id": 1,
        "market_id": "0xabc",
        "market_slug": "slug",
        # confidence_score and the embedded report_json are kept in sync, the
        # way record_evaluation/record_checkpoint always update both — the
        # report's own confidence is the current (adjusted) value.
        "report_json": _report(confidence=0.9).model_dump(mode="json"),
        "price_at_report": 0.5,
        "created_at": "2026-08-13T12:00:00+00:00",
        "evaluated_at": "2026-08-15T12:00:00+00:00",
        "outcome": "CONFIRMED",
        "confidence_score": 0.9,
        "stated_confidence_score": 0.8,
    }
    row.update(overrides)
    return row


class TestGetScoredReports:
    def test_filters_on_outcome_not_null(self):
        store = RecordingStore(response=[[]])
        store.get_scored_reports()
        params = store.calls[0]["params"]
        assert params["outcome"] == "not.is.null"

    def test_decodes_stated_confidence(self):
        store = RecordingStore(response=[[_row()]])
        scored = store.get_scored_reports()
        assert scored[0].stated_confidence == 0.8
        assert scored[0].report.confidence_score == 0.9


class TestToStoredStatedConfidenceFallback:
    def test_falls_back_to_confidence_score_when_column_is_absent(self):
        # A deployment that hasn't run the stated_confidence_score migration
        # yet must not 500 on every read — PostgREST simply omits the key.
        store = RecordingStore(response=[[]])
        row = _row()
        del row["stated_confidence_score"]
        stored = store._to_stored(row)
        assert stored.stated_confidence == row["confidence_score"]

    def test_falls_back_when_column_is_null(self):
        store = RecordingStore(response=[[]])
        stored = store._to_stored(_row(stated_confidence_score=None))
        assert stored.stated_confidence == 0.9


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


class TestProductionRefusesSqliteFallback:
    """B.13: production must fail startup rather than degrade to SQLite.

    SQLite on a deployed instance sits on an ephemeral container disk — every
    restart loses the price observed at report time, and that price cannot be
    reconstructed later. The precedent this guards against is the 17-day
    silent record_usage failure: a write that fails quietly instead of
    stopping the process.
    """

    def test_missing_config_in_production_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMIE_ENVIRONMENT", "production")
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
        with pytest.raises(MemoryStoreError, match="production"):
            build_memory_store(str(tmp_path / "unused.db"))

    def test_unreachable_postgres_in_production_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMIE_ENVIRONMENT", "production")
        monkeypatch.setenv("SUPABASE_URL", "https://example.test")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")

        def unreachable(*args, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "request", unreachable)

        with pytest.raises(MemoryStoreError):
            build_memory_store(str(tmp_path / "unused.db"))

    def test_reachable_postgres_in_production_is_used(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMIE_ENVIRONMENT", "production")
        monkeypatch.setenv("SUPABASE_URL", "https://example.test")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")

        class FakeResponse:
            status_code = 200
            text = "[]"

            @staticmethod
            def json():
                return []

        monkeypatch.setattr(httpx, "request", lambda *a, **k: FakeResponse())

        store = build_memory_store(str(tmp_path / "unused.db"))
        assert isinstance(store, PostgresMemoryStore)

    def test_non_production_is_unaffected(self, tmp_path, monkeypatch):
        # Sanity check: the new production gate must not change behaviour
        # anywhere else. No connectivity probe, no PMIE_ENVIRONMENT set.
        monkeypatch.delenv("PMIE_ENVIRONMENT", raising=False)
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        assert isinstance(
            build_memory_store(str(tmp_path / "local.db")), SqliteMemoryStore
        )
