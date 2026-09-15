import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "1")
    return TestClient(create_app(db_path=str(tmp_path / "calibration.db")))


class FakeStore:
    def __init__(self, scored=None):
        self._scored = scored or []

    def get_scored_confidence_outcomes(self):
        return self._scored


def test_public_without_any_token(client):
    """The reliability curve is a trust signal for a visitor with no
    account, same as growth_routes.py's public routes."""
    client.app.state.auth_disabled = False
    response = client.get("/v1/calibration")
    assert response.status_code == 200


def test_empty_store_is_the_insufficient_data_state(client):
    client.app.state.memory_store = FakeStore(scored=[])
    response = client.get("/v1/calibration")
    body = response.json()
    assert response.status_code == 200
    assert body["sufficient"] is False
    assert body["bins"] == []
    assert body["total_scored"] == 0
    assert body["minimum_for_display"] == 300


def test_never_returns_a_curve_below_the_gate(client):
    client.app.state.memory_store = FakeStore(
        scored=[(0.8, "CONFIRMED") for _ in range(300)]
    )
    response = client.get("/v1/calibration")
    body = response.json()
    assert body["sufficient"] is False
    assert body["bins"] == []
    assert body["total_scored"] == 300


def test_returns_a_curve_once_past_the_gate(client):
    scored = [(0.8, "CONFIRMED") for _ in range(250)] + [
        (0.8, "REVERSED") for _ in range(51)
    ]
    client.app.state.memory_store = FakeStore(scored=scored)

    response = client.get("/v1/calibration")
    body = response.json()

    assert body["sufficient"] is True
    assert body["total_scored"] == 301
    assert len(body["bins"]) == 1
    bin_80 = body["bins"][0]
    assert bin_80["lower"] == 0.8
    assert bin_80["upper"] == 0.9
    assert bin_80["sample_size"] == 301
    assert bin_80["observed_accuracy"] == pytest.approx(250 / 301)


def test_response_carries_a_generated_at_timestamp(client):
    client.app.state.memory_store = FakeStore(scored=[])
    response = client.get("/v1/calibration")
    assert response.json()["generated_at"]


def test_never_returns_a_forecast_field(client):
    # PMIE scores its own confidence and never forecasts outcomes — no field
    # here should imply otherwise.
    client.app.state.memory_store = FakeStore(scored=[])
    body = client.get("/v1/calibration").json()
    forbidden = {"probability", "forecast", "prediction", "edge"}
    assert forbidden.isdisjoint(body.keys())
    for b in body["bins"]:
        assert forbidden.isdisjoint(b.keys())


class CountingFakeStore(FakeStore):
    """Counts reads, so a test can prove the cache skipped a call to it."""

    def __init__(self, scored=None):
        super().__init__(scored)
        self.reads = 0

    def get_scored_confidence_outcomes(self):
        self.reads += 1
        return self._scored


class TestCache:
    """B.11 review: an unauthenticated route reading every scored row on
    every hit is a cost/DoS surface. A short TTL cache bounds that."""

    def test_repeated_requests_within_the_ttl_hit_the_store_once(self, client):
        store = CountingFakeStore(scored=[(0.8, "CONFIRMED")])
        client.app.state.memory_store = store

        client.get("/v1/calibration")
        client.get("/v1/calibration")
        client.get("/v1/calibration")

        assert store.reads == 1

    def test_a_request_past_the_ttl_reads_again(self, client, monkeypatch):
        import time

        import src.api.calibration_routes as calibration_routes

        store = CountingFakeStore(scored=[(0.8, "CONFIRMED")])
        client.app.state.memory_store = store

        fake_now = [1_000_000.0]
        monkeypatch.setattr(time, "time", lambda: fake_now[0])

        client.get("/v1/calibration")
        fake_now[0] += calibration_routes.CACHE_TTL_SECONDS + 1
        client.get("/v1/calibration")

        assert store.reads == 2

    def test_two_apps_do_not_share_a_cache(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PMIE_AUTH_DISABLED", "1")
        from src.api.app import create_app

        client_a = TestClient(create_app(db_path=str(tmp_path / "a.db")))
        client_b = TestClient(create_app(db_path=str(tmp_path / "b.db")))

        store_a = CountingFakeStore(scored=[(0.8, "CONFIRMED")])
        store_b = CountingFakeStore(scored=[(0.2, "REVERSED")])
        client_a.app.state.memory_store = store_a
        client_b.app.state.memory_store = store_b

        client_a.get("/v1/calibration")
        client_b.get("/v1/calibration")

        assert store_a.reads == 1
        assert store_b.reads == 1
