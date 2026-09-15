"""GET /v1/me — ROADMAP B.19 review: ui_mode must round-trip on the profile.

Also covers the B.10 review: /me must report the real effective allowance
(free tier + referral bonus) and whether enforcement applies (exempt for
grandfathered accounts), and must sync referral attribution/conversion.
"""

import pytest
from fastapi.testclient import TestClient

from src.api.accounts import Profile, ReferralCounts
from src.api.app import create_app
from src.api.auth import CurrentUser


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "1")
    return TestClient(create_app(db_path=str(tmp_path / "me.db")))


@pytest.fixture
def authed_client(monkeypatch, tmp_path):
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "0")
    monkeypatch.delenv("PMIE_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    return TestClient(create_app(db_path=str(tmp_path / "me_authed.db")))


class FakeJwks:
    def __init__(self, email="user-1@example.test", email_verified=False):
        self._email = email
        self._email_verified = email_verified

    def verify(self, token: str) -> CurrentUser:
        # aal2 so the MFA step-up gate (unrelated to this suite) never engages.
        return CurrentUser(
            id=token,
            email=self._email,
            aal="aal2",
            email_verified=self._email_verified,
        )


class FakeAccounts:
    def __init__(
        self,
        ui_mode=None,
        is_grandfathered=False,
        counts=None,
        referred_by=None,
        attribution_result=True,
    ):
        self.configured = True
        self.profile = Profile(
            id="00000000-0000-0000-0000-000000000000",
            display_name=None,
            is_invited=True,
            is_grandfathered=is_grandfathered,
            onboarding_completed_at=None,
            ui_mode=ui_mode,
            referred_by=referred_by,
        )
        self.counts = counts or ReferralCounts(referred_count=0, converted_count=0)
        self.attribution_calls: list[tuple] = []
        self.conversion_calls: list[str] = []
        self._attribution_result = attribution_result

    def get_profile(self, profile_id):
        return self.profile

    def get_interests(self, profile_id):
        return []

    def monthly_usage(self, profile_id):
        return 0

    def referral_counts(self, profile_id):
        return self.counts

    def sync_referral_attribution(self, profile, email):
        self.attribution_calls.append((profile.id, email))
        return self._attribution_result

    def mark_referral_converted(self, profile_id):
        self.conversion_calls.append(profile_id)


def test_me_reports_the_chosen_ui_mode(client):
    client.app.state.accounts = FakeAccounts(ui_mode="TERMINAL")
    response = client.get("/v1/me")
    assert response.status_code == 200
    assert response.json()["ui_mode"] == "TERMINAL"


def test_me_reports_null_when_never_chosen(client):
    client.app.state.accounts = FakeAccounts(ui_mode=None)
    response = client.get("/v1/me")
    assert response.status_code == 200
    assert response.json()["ui_mode"] is None


def test_me_reports_the_effective_allowance_including_bonus(client):
    client.app.state.accounts = FakeAccounts(
        counts=ReferralCounts(referred_count=6, converted_count=6)
    )
    response = client.get("/v1/me")
    assert response.status_code == 200
    assert response.json()["usage"]["free_monthly_allowance"] == 8  # 5 + 3


def test_me_enforced_is_true_for_an_ordinary_account(client):
    client.app.state.accounts = FakeAccounts(is_grandfathered=False)
    response = client.get("/v1/me")
    assert response.json()["usage"]["enforced"] is True


def test_me_enforced_is_false_for_a_grandfathered_account(client):
    client.app.state.accounts = FakeAccounts(is_grandfathered=True)
    response = client.get("/v1/me")
    assert response.json()["usage"]["enforced"] is False


def test_me_syncs_referral_attribution_with_the_caller_s_email(authed_client):
    authed_client.app.state.jwks = FakeJwks(email="user-1@example.test")
    fake = FakeAccounts()
    authed_client.app.state.accounts = fake

    authed_client.get("/v1/me", headers={"authorization": "Bearer user-1"})

    assert fake.attribution_calls == [(fake.profile.id, "user-1@example.test")]


def test_me_marks_referral_converted_when_the_caller_is_verified(authed_client):
    authed_client.app.state.jwks = FakeJwks(email_verified=True)
    fake = FakeAccounts()
    authed_client.app.state.accounts = fake

    authed_client.get("/v1/me", headers={"authorization": "Bearer user-1"})

    assert fake.conversion_calls == [fake.profile.id]


def test_me_does_not_mark_converted_when_the_caller_is_unverified(authed_client):
    authed_client.app.state.jwks = FakeJwks(email_verified=False)
    fake = FakeAccounts()
    authed_client.app.state.accounts = fake

    authed_client.get("/v1/me", headers={"authorization": "Bearer user-1"})

    assert fake.conversion_calls == []


def test_me_does_not_mark_converted_when_never_referred(authed_client):
    """Most users were never referred at all — a verified-email load must

    not PATCH a referrals row that doesn't exist for them.
    """
    authed_client.app.state.jwks = FakeJwks(email_verified=True)
    fake = FakeAccounts(referred_by=None, attribution_result=False)
    authed_client.app.state.accounts = fake

    authed_client.get("/v1/me", headers={"authorization": "Bearer user-1"})

    assert fake.conversion_calls == []


def test_me_marks_converted_when_already_referred_before_this_load(authed_client):
    """profile.referred_by was already set on a prior load — sync_referral_attribution

    short-circuits (no attribution work to redo), but conversion must still fire.
    """
    authed_client.app.state.jwks = FakeJwks(email_verified=True)
    fake = FakeAccounts(referred_by="referrer-1", attribution_result=True)
    authed_client.app.state.accounts = fake

    authed_client.get("/v1/me", headers={"authorization": "Bearer user-1"})

    assert fake.conversion_calls == [fake.profile.id]
