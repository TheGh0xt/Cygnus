"""POST /v1/analyses quota enforcement — ROADMAP B.10.

monthly_usage() existed before this and was reported on /v1/me, but nothing
ever compared it to the allowance: a user who exhausted their free tier could
keep starting analyses indefinitely. This closes that gap.
"""

import pytest
from fastapi.testclient import TestClient

from src.api.accounts import AccountsError, Profile, ReferralCounts
from src.api.app import create_app
from src.api.auth import CurrentUser


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "1")
    return TestClient(create_app(db_path=str(tmp_path / "quota.db")))


@pytest.fixture
def authed_client(monkeypatch, tmp_path):
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "0")
    monkeypatch.delenv("PMIE_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    return TestClient(create_app(db_path=str(tmp_path / "quota_authed.db")))


class FakeJwks:
    def verify(self, token: str) -> CurrentUser:
        # aal2 so the MFA step-up gate (unrelated to this suite) never engages.
        return CurrentUser(id=token, email=None, aal="aal2")


class FakeAccounts:
    def __init__(
        self,
        used=0,
        counts=None,
        error=None,
        configured=True,
        is_grandfathered=False,
    ):
        self.used = used
        self.counts = counts or ReferralCounts(referred_count=0, converted_count=0)
        self.error = error
        self.configured = configured
        self.is_grandfathered = is_grandfathered

    def get_profile(self, profile_id):
        return Profile(
            id=profile_id,
            display_name=None,
            is_invited=True,
            is_grandfathered=self.is_grandfathered,
            onboarding_completed_at=None,
        )

    def monthly_usage(self, profile_id):
        if self.error:
            raise self.error
        return self.used

    def referral_counts(self, profile_id):
        if self.error:
            raise self.error
        return self.counts


_BODY = {"query": "why is world-cup-winner moving?"}


def test_allows_a_request_under_the_free_allowance(client):
    client.app.state.accounts = FakeAccounts(used=4)
    response = client.post("/v1/analyses", json=_BODY)
    assert response.status_code == 201


def test_rejects_a_request_at_the_free_allowance(client):
    client.app.state.accounts = FakeAccounts(used=5)
    response = client.post("/v1/analyses", json=_BODY)
    assert response.status_code == 403
    assert response.json()["type"].endswith("quota-exceeded")


def test_referral_bonus_raises_the_allowance(client):
    client.app.state.accounts = FakeAccounts(
        used=5, counts=ReferralCounts(referred_count=6, converted_count=6)
    )
    response = client.post("/v1/analyses", json=_BODY)
    assert response.status_code == 201


def test_bonus_still_runs_out(client):
    client.app.state.accounts = FakeAccounts(
        used=8, counts=ReferralCounts(referred_count=6, converted_count=6)
    )
    response = client.post("/v1/analyses", json=_BODY)
    assert response.status_code == 403


def test_unconfigured_accounts_do_not_block_dev_usage(client):
    client.app.state.accounts = FakeAccounts(used=999, configured=False)
    response = client.post("/v1/analyses", json=_BODY)
    assert response.status_code == 201


def test_store_failure_is_503(client):
    client.app.state.accounts = FakeAccounts(error=AccountsError("unreachable"))
    response = client.post("/v1/analyses", json=_BODY)
    assert response.status_code == 503


def test_quota_exceeded_names_the_pro_price(client):
    client.app.state.accounts = FakeAccounts(used=5)
    response = client.post("/v1/analyses", json=_BODY)
    assert response.status_code == 403
    assert "$19" in response.json()["detail"]


def test_grandfathered_accounts_are_exempt_from_the_quota(authed_client):
    authed_client.app.state.jwks = FakeJwks()
    authed_client.app.state.accounts = FakeAccounts(used=999, is_grandfathered=True)

    response = authed_client.post(
        "/v1/analyses", headers={"authorization": "Bearer user-1"}, json=_BODY
    )
    assert response.status_code == 201
