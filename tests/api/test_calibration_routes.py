from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "1")
    return TestClient(create_app(db_path=str(tmp_path / "calibration.db")))


@dataclass
class FakeScoredReport:
    stated_confidence: float
    outcome: str


class FakeStore:
    def __init__(self, scored=None):
        self._scored = scored or []

    def get_scored_reports(self):
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
        scored=[FakeScoredReport(0.8, "CONFIRMED") for _ in range(300)]
    )
    response = client.get("/v1/calibration")
    body = response.json()
    assert body["sufficient"] is False
    assert body["bins"] == []
    assert body["total_scored"] == 300


def test_returns_a_curve_once_past_the_gate(client):
    scored = [FakeScoredReport(0.8, "CONFIRMED") for _ in range(250)] + [
        FakeScoredReport(0.8, "REVERSED") for _ in range(51)
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
