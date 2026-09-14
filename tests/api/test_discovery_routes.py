import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.generation.discovery import DiscoveryError
from src.generation.selector import Candidate


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "1")
    return TestClient(create_app(db_path=str(tmp_path / "discovery.db")))


class FakeAccounts:
    def __init__(self, interests=None, error=None):
        self.interests = interests if interests is not None else ["politics", "crypto"]
        self.error = error
        self.configured = True

    def get_interests(self, profile_id):
        if self.error:
            raise self.error
        return self.interests


class FakeDiscovery:
    def __init__(self, candidates=None, error=None):
        self.candidates = candidates or []
        self.error = error
        self.calls: list[tuple] = []

    async def moving_markets(self, categories=None, limit=20):
        self.calls.append((categories, limit))
        if self.error:
            raise self.error
        return self.candidates


def test_requires_a_token(client):
    """Auth-disabled fixture aside, the router itself must close by default."""
    client.app.state.auth_disabled = False
    response = client.get("/v1/markets/moving")
    assert response.status_code == 401


def test_returns_markets_ranked_by_movement(client):
    client.app.state.accounts = FakeAccounts(interests=["politics"])
    client.app.state.discovery = FakeDiscovery(
        candidates=[
            Candidate("m1", "Market one", "politics", 0.4, 0.2, 500.0, None),
        ]
    )

    response = client.get("/v1/markets/moving")

    assert response.status_code == 200
    body = response.json()
    assert body["categories"] == ["politics"]
    assert body["markets"][0]["slug"] == "m1"
    assert body["markets"][0]["source"] == "POLYMARKET"


def test_uses_the_caller_s_interests_as_the_category_filter(client):
    fake_discovery = FakeDiscovery()
    client.app.state.accounts = FakeAccounts(interests=["ai", "sports"])
    client.app.state.discovery = fake_discovery

    client.get("/v1/markets/moving")

    categories, _ = fake_discovery.calls[0]
    assert categories == ["ai", "sports"]


def test_no_chosen_categories_means_an_unfiltered_feed(client):
    fake_discovery = FakeDiscovery()
    client.app.state.accounts = FakeAccounts(interests=[])
    client.app.state.discovery = fake_discovery

    response = client.get("/v1/markets/moving")

    assert response.status_code == 200
    categories, _ = fake_discovery.calls[0]
    assert categories is None


def test_discovery_failure_is_503(client):
    client.app.state.accounts = FakeAccounts()
    client.app.state.discovery = FakeDiscovery(error=DiscoveryError("unreachable"))

    response = client.get("/v1/markets/moving")

    assert response.status_code == 503
    assert response.json()["type"].endswith("sagittarius-unavailable")
