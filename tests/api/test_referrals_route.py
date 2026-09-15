"""GET /v1/me/referrals — ROADMAP B.10."""

import pytest
from fastapi.testclient import TestClient

from src.api.accounts import AccountsError, ReferralCounts
from src.api.app import create_app

_USER_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "1")
    return TestClient(create_app(db_path=str(tmp_path / "referrals.db")))


class FakeAccounts:
    def __init__(self, code="ABCD1234", counts=None, error=None, configured=True):
        self.code = code
        self.counts = counts or ReferralCounts(referred_count=0, converted_count=0)
        self.error = error
        self.configured = configured

    def get_referral_code(self, profile_id):
        if self.error:
            raise self.error
        return self.code

    def referral_counts(self, profile_id):
        if self.error:
            raise self.error
        return self.counts


def test_requires_a_token(client):
    client.app.state.auth_disabled = False
    response = client.get("/v1/me/referrals")
    assert response.status_code == 401


def test_returns_the_code_and_standing(client):
    client.app.state.accounts = FakeAccounts(
        code="WXYZ9876",
        counts=ReferralCounts(referred_count=3, converted_count=0),
    )
    response = client.get("/v1/me/referrals")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "code": "WXYZ9876",
        "referred_count": 3,
        "converted_count": 0,
        "analyses_granted": 0,
        "next_reward_at": 6,
    }


def test_bonus_reflects_converted_referrals(client):
    client.app.state.accounts = FakeAccounts(
        counts=ReferralCounts(referred_count=7, converted_count=6)
    )
    response = client.get("/v1/me/referrals")
    body = response.json()
    assert body["analyses_granted"] == 3
    assert body["next_reward_at"] == 12


def test_not_configured_is_503(client):
    client.app.state.accounts = FakeAccounts(configured=False)
    response = client.get("/v1/me/referrals")
    assert response.status_code == 503


def test_store_failure_is_503(client):
    client.app.state.accounts = FakeAccounts(error=AccountsError("unreachable"))
    response = client.get("/v1/me/referrals")
    assert response.status_code == 503
