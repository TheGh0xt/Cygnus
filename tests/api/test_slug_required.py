"""An analysis needs a market to analyse.

A real request — "who will win the ballon dor?" — contained no slug, so the
retrieval stages had nothing to look up. The analyst correctly reported that
it had no market data, but only after four model calls and a minute of
waiting, and the run stored a row with an empty market_id that can never be
scored.

Both ends are now closed: the request is rejected up front, and an
unscoreable report is never stored even if one somehow reaches persistence.
"""

import os

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.models import extract_slug
from src.api.persistence import ReportPersistence


@pytest.fixture
def client(tmp_path):
    os.environ["PMIE_AUTH_DISABLED"] = "1"
    return TestClient(create_app(db_path=str(tmp_path / "slug.db")))


class TestRequestsWithoutAMarket:
    @pytest.mark.parametrize(
        "query",
        [
            "who will win the ballon dor?",
            "what is moving today?",
            "explain the market",
            "why did it go up",
        ],
    )
    def test_rejected_before_any_work_starts(self, client, query):
        response = client.post("/v1/analyses", json={"query": query})
        assert response.status_code == 422, (
            f"{query!r} has no identifiable market and must not start a run"
        )

    def test_rejection_explains_what_to_provide(self, client):
        body = client.post(
            "/v1/analyses", json={"query": "who will win the ballon dor?"}
        ).json()
        assert body["type"].endswith("invalid-request")
        # The message has to be actionable, not merely correct.
        assert "slug" in body["detail"].lower()
        assert "polymarket.com/event/" in body["detail"]

    def test_no_usage_is_recorded_for_a_rejected_request(self, client):
        # A rejected request costs nothing and must not count against anyone.
        response = client.post("/v1/analyses", json={"query": "explain it"})
        assert response.status_code == 422


class TestRequestsWithAMarket:
    @pytest.mark.parametrize(
        "query",
        [
            "why is world-cup-winner moving?",
            "https://polymarket.com/event/world-cup-winner",
            "what happened to ballon-dor-winner-2026 today?",
        ],
    )
    def test_accepted(self, client, query):
        assert client.post("/v1/analyses", json={"query": query}).status_code == 201

    def test_explicit_slug_field_is_enough(self, client):
        response = client.post(
            "/v1/analyses",
            json={"query": "who will win this?", "slug": "world-cup-winner"},
        )
        assert response.status_code == 201, (
            "an explicit slug must satisfy the check even when the prose has none"
        )


class TestExtractSlugDirectly:
    def test_returns_empty_for_prose_without_a_market(self):
        assert extract_slug("who will win the ballon dor?", None) == ""


class TestUnscoreableReportsAreNotStored:
    def test_report_without_market_id_or_slug_is_skipped(self, caplog):
        class RecordingStore:
            def __init__(self):
                self.saved = []

            def save_report(self, report, slug, price, created_at=None):
                self.saved.append((report, slug, price))

        store = RecordingStore()
        persistence = ReportPersistence(store, price_fetcher=None)

        with caplog.at_level("WARNING"):
            persistence.save(
                {
                    "market_id": "",
                    "timestamp": "2026-08-14T00:00:00+00:00",
                    "summary": "no market data was available",
                    "primary_causal_driver": "UNKNOWN_ANOMALY",
                    "confidence_score": 0.5,
                    "key_drivers": [],
                },
                slug="",
            )

        assert store.saved == [], "an unscoreable report must not be stored"
        assert "unscoreable" in caplog.text
