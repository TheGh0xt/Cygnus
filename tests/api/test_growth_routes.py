import pytest
from fastapi.testclient import TestClient

from src.api.accounts import AccountsError
from src.api.app import create_app
from src.api.auth import CurrentUser
from src.api.growth import GrowthError, WaitlistJoinResult


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "0")
    monkeypatch.delenv("PMIE_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    return TestClient(create_app(db_path=str(tmp_path / "growth.db")))


class FakeGrowth:
    def __init__(self, result=None, error=None, event_error=None, order=None):
        self.result = result or WaitlistJoinResult(already_registered=False)
        self.error = error
        self.event_error = event_error
        self.configured = True
        self.calls: list[tuple[str, str | None]] = []
        self.event_calls: list[tuple] = []
        self._order = order

    def join_waitlist(self, email, referral_code):
        self.calls.append((email, referral_code))
        if self.error:
            raise self.error
        return self.result

    def record_event(self, profile_id, name, ui_mode, properties):
        self.event_calls.append((profile_id, name, ui_mode, properties))
        if self._order is not None:
            self._order.append("record_event")
        if self.event_error:
            raise self.event_error


class FakeAccountsForEvents:
    def __init__(self, error=None, order=None):
        self.configured = True
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self._order = order

    def set_ui_mode(self, profile_id, ui_mode):
        self.calls.append((profile_id, ui_mode))
        if self._order is not None:
            self._order.append("set_ui_mode")
        if self.error:
            raise self.error


class FakeJwks:
    """A bearer token here is literally the caller's user id, at aal2 so the

    aal2-enforcement gate (unrelated to this suite) never engages.
    """

    def verify(self, token: str) -> CurrentUser:
        return CurrentUser(id=token, email=None, aal="aal2")


def _auth(user: str = "user-1") -> dict:
    return {"authorization": f"Bearer {user}"}


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


class TestEventsRoute:
    def test_requires_a_token(self, client):
        client.app.state.growth = FakeGrowth()
        response = client.post("/v1/events", json={"name": "ui_mode_switched"})
        assert response.status_code == 401

    def test_records_the_event_for_the_caller(self, client):
        client.app.state.jwks = FakeJwks()
        fake = FakeGrowth()
        client.app.state.growth = fake
        client.app.state.accounts = FakeAccountsForEvents()

        response = client.post(
            "/v1/events",
            headers=_auth("user-1"),
            json={
                "name": "ui_mode_switched",
                "ui_mode": "TERMINAL",
                "properties": {"from": "CONVENTIONAL"},
            },
        )

        assert response.status_code == 204
        assert fake.event_calls == [
            ("user-1", "ui_mode_switched", "TERMINAL", {"from": "CONVENTIONAL"})
        ]

    def test_ui_mode_and_properties_are_optional(self, client):
        client.app.state.jwks = FakeJwks()
        fake = FakeGrowth()
        client.app.state.growth = fake

        response = client.post(
            "/v1/events", headers=_auth("user-1"), json={"name": "analysis_started"}
        )

        assert response.status_code == 204
        assert fake.event_calls == [("user-1", "analysis_started", None, {})]

    def test_a_caller_cannot_record_for_someone_else(self, client):
        """profile_id always comes from the verified token, never the body —

        there is no profile_id field on the request at all.
        """
        client.app.state.jwks = FakeJwks()
        fake = FakeGrowth()
        client.app.state.growth = fake

        client.post(
            "/v1/events", headers=_auth("user-1"), json={"name": "analysis_started"}
        )
        client.post(
            "/v1/events", headers=_auth("user-2"), json={"name": "analysis_started"}
        )

        assert [c[0] for c in fake.event_calls] == ["user-1", "user-2"]

    def test_not_configured_is_503(self, client):
        client.app.state.jwks = FakeJwks()
        fake = FakeGrowth()
        fake.configured = False
        client.app.state.growth = fake

        response = client.post(
            "/v1/events", headers=_auth("user-1"), json={"name": "analysis_started"}
        )
        assert response.status_code == 503

    def test_store_failure_is_503(self, client):
        client.app.state.jwks = FakeJwks()
        fake = FakeGrowth(event_error=GrowthError("unreachable"))
        client.app.state.growth = fake

        response = client.post(
            "/v1/events", headers=_auth("user-1"), json={"name": "analysis_started"}
        )
        assert response.status_code == 503

    def test_rejects_a_malformed_name(self, client):
        """Only lowercase snake_case, e.g. 'ui_mode_switched' — never free text."""
        client.app.state.jwks = FakeJwks()
        client.app.state.growth = FakeGrowth()

        response = client.post(
            "/v1/events", headers=_auth("user-1"), json={"name": "Not An Event!"}
        )
        assert response.status_code == 422

    def test_rejects_too_many_properties(self, client):
        client.app.state.jwks = FakeJwks()
        client.app.state.growth = FakeGrowth()
        properties = {f"k{i}": "v" for i in range(21)}

        response = client.post(
            "/v1/events",
            headers=_auth("user-1"),
            json={"name": "analysis_started", "properties": properties},
        )
        assert response.status_code == 422

    def test_rejects_an_oversized_property_value(self, client):
        client.app.state.jwks = FakeJwks()
        client.app.state.growth = FakeGrowth()

        response = client.post(
            "/v1/events",
            headers=_auth("user-1"),
            json={"name": "analysis_started", "properties": {"note": "x" * 501}},
        )
        assert response.status_code == 422

    def test_rejects_an_oversized_property_key(self, client):
        client.app.state.jwks = FakeJwks()
        client.app.state.growth = FakeGrowth()

        response = client.post(
            "/v1/events",
            headers=_auth("user-1"),
            json={"name": "analysis_started", "properties": {"k" * 65: "v"}},
        )
        assert response.status_code == 422

    def test_valid_properties_still_pass(self, client):
        client.app.state.jwks = FakeJwks()
        fake = FakeGrowth()
        client.app.state.growth = fake

        response = client.post(
            "/v1/events",
            headers=_auth("user-1"),
            json={"name": "analysis_started", "properties": {"source": "feed"}},
        )
        assert response.status_code == 204

    def test_ui_mode_switched_persists_the_profile_choice(self, client):
        """The point of Profile.ui_mode/MeResponse.ui_mode: a switch event

        must actually update the profile, not just get logged.
        """
        client.app.state.jwks = FakeJwks()
        client.app.state.growth = FakeGrowth()
        fake_accounts = FakeAccountsForEvents()
        client.app.state.accounts = fake_accounts

        response = client.post(
            "/v1/events",
            headers=_auth("user-1"),
            json={"name": "ui_mode_switched", "ui_mode": "TERMINAL"},
        )

        assert response.status_code == 204
        assert fake_accounts.calls == [("user-1", "TERMINAL")]

    def test_other_events_do_not_touch_the_profile(self, client):
        client.app.state.jwks = FakeJwks()
        client.app.state.growth = FakeGrowth()
        fake_accounts = FakeAccountsForEvents()
        client.app.state.accounts = fake_accounts

        client.post(
            "/v1/events", headers=_auth("user-1"), json={"name": "analysis_started"}
        )

        assert fake_accounts.calls == []

    def test_ui_mode_switched_without_a_mode_does_not_touch_the_profile(self, client):
        """Malformed but not rejected by the schema: no mode chosen, nothing to persist."""
        client.app.state.jwks = FakeJwks()
        client.app.state.growth = FakeGrowth()
        fake_accounts = FakeAccountsForEvents()
        client.app.state.accounts = fake_accounts

        client.post(
            "/v1/events", headers=_auth("user-1"), json={"name": "ui_mode_switched"}
        )

        assert fake_accounts.calls == []

    def test_ui_mode_switched_without_a_mode_is_rejected(self, client):
        """Follow-up from the Cygnus#32 review: a switch event with no mode

        chosen was previously accepted silently and updated nothing.
        """
        client.app.state.jwks = FakeJwks()
        client.app.state.growth = FakeGrowth()
        client.app.state.accounts = FakeAccountsForEvents()

        response = client.post(
            "/v1/events", headers=_auth("user-1"), json={"name": "ui_mode_switched"}
        )
        assert response.status_code == 422

    def test_profile_is_updated_before_the_event_is_logged(self, client):
        """Follow-up from the Cygnus#32 review: the profile PATCH is

        idempotent but event logging is not, so on a retry after a failure
        the idempotent write must happen first — otherwise a failed PATCH
        after a successful log means a retry double-logs the event.
        """
        client.app.state.jwks = FakeJwks()
        order: list[str] = []
        client.app.state.growth = FakeGrowth(order=order)
        client.app.state.accounts = FakeAccountsForEvents(order=order)

        response = client.post(
            "/v1/events",
            headers=_auth("user-1"),
            json={"name": "ui_mode_switched", "ui_mode": "TERMINAL"},
        )

        assert response.status_code == 204
        assert order == ["set_ui_mode", "record_event"]

    def test_profile_update_failure_is_503(self, client):
        client.app.state.jwks = FakeJwks()
        client.app.state.growth = FakeGrowth()
        client.app.state.accounts = FakeAccountsForEvents(
            error=AccountsError("unreachable")
        )

        response = client.post(
            "/v1/events",
            headers=_auth("user-1"),
            json={"name": "ui_mode_switched", "ui_mode": "TERMINAL"},
        )
        assert response.status_code == 503
