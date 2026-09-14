import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.growth import GrowthError, WaitlistJoinResult


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "0")
    monkeypatch.delenv("PMIE_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    return TestClient(create_app(db_path=str(tmp_path / "growth.db")))


class FakeGrowth:
    def __init__(self, result=None, error=None):
        self.result = result or WaitlistJoinResult(already_registered=False)
        self.error = error
        self.configured = True
        self.calls: list[tuple[str, str | None]] = []

    def join_waitlist(self, email, referral_code):
        self.calls.append((email, referral_code))
        if self.error:
            raise self.error
        return self.result


def test_join_waitlist_is_public(client):
    """No credentials required: this is a pre-signup surface."""
    client.app.state.growth = FakeGrowth()
    response = client.post("/v1/waitlist", json={"email": "someone@example.com"})
    assert response.status_code == 200


def test_join_waitlist_returns_the_declared_shape(client):
    fake = FakeGrowth()
    client.app.state.growth = fake
    response = client.post(
        "/v1/waitlist", json={"email": "someone@example.com", "referral_code": "r1"}
    )
    body = response.json()
    assert body == {"position": None, "already_registered": False}
    assert fake.calls == [("someone@example.com", "r1")]


def test_join_waitlist_reports_already_registered(client):
    client.app.state.growth = FakeGrowth(
        result=WaitlistJoinResult(already_registered=True)
    )
    response = client.post("/v1/waitlist", json={"email": "someone@example.com"})
    assert response.json()["already_registered"] is True


def test_join_waitlist_rejects_malformed_email(client):
    client.app.state.growth = FakeGrowth()
    response = client.post("/v1/waitlist", json={"email": "not-an-email"})
    assert response.status_code == 422
    assert response.json()["type"].endswith("invalid-request")


def test_join_waitlist_surfaces_store_failure_as_503(client):
    client.app.state.growth = FakeGrowth(error=GrowthError("unreachable"))
    response = client.post("/v1/waitlist", json={"email": "someone@example.com"})
    assert response.status_code == 503


def test_join_waitlist_not_configured_is_503(client):
    fake = FakeGrowth()
    fake.configured = False
    client.app.state.growth = fake
    response = client.post("/v1/waitlist", json={"email": "someone@example.com"})
    assert response.status_code == 503
