"""POST /v1/analyses quota enforcement — ROADMAP B.10.

monthly_usage() existed before this and was reported on /v1/me, but nothing
ever compared it to the allowance: a user who exhausted their free tier could
keep starting analyses indefinitely. This closes that gap.
"""

import pytest
from fastapi.testclient import TestClient

from src.api.accounts import AccountsError, ReferralCounts
from src.api.app import create_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "1")
    return TestClient(create_app(db_path=str(tmp_path / "quota.db")))


class FakeAccounts:
    def __init__(self, used=0, counts=None, error=None, configured=True):
        self.used = used
        self.counts = counts or ReferralCounts(referred_count=0, converted_count=0)
        self.error = error
        self.configured = configured

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
