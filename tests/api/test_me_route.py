"""GET /v1/me — ROADMAP B.19 review: ui_mode must round-trip on the profile."""

import pytest
from fastapi.testclient import TestClient

from src.api.accounts import Profile
from src.api.app import create_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "1")
    return TestClient(create_app(db_path=str(tmp_path / "me.db")))


class FakeAccounts:
    def __init__(self, ui_mode=None):
        self.configured = True
        self.profile = Profile(
            id="00000000-0000-0000-0000-000000000000",
            display_name=None,
            is_invited=True,
            is_grandfathered=False,
            onboarding_completed_at=None,
            ui_mode=ui_mode,
        )

    def get_profile(self, profile_id):
        return self.profile

    def get_interests(self, profile_id):
        return []

    def monthly_usage(self, profile_id):
        return 0


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
