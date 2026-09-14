import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.mfa import EnrollResult, FactorStatus, InvalidCode, MfaError


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "1")
    return TestClient(create_app(db_path=str(tmp_path / "mfa.db")))


class FakeMfa:
    def __init__(self, enroll_result=None, verify_error=None, status_result=None):
        self.configured = True
        self.enroll_result = enroll_result or EnrollResult(
            factor_id="factor-1", secret="BASE32SECRET", qr_uri="otpauth://totp/x"
        )
        self.verify_error = verify_error
        self.status_result = status_result or FactorStatus(
            enrolled=False, verified_at=None
        )
        self.enroll_calls: list[str] = []
        self.verify_calls: list[tuple[str, str, str]] = []
        self.status_calls: list[str] = []

    def enroll(self, user_token):
        self.enroll_calls.append(user_token)
        return self.enroll_result

    def verify(self, user_token, factor_id, code):
        self.verify_calls.append((user_token, factor_id, code))
        if self.verify_error:
            raise self.verify_error

    def status(self, user_token):
        self.status_calls.append(user_token)
        return self.status_result


AUTH = {"authorization": "Bearer test-user-token"}


class TestEnrollRoute:
    def test_requires_a_token(self, client):
        client.app.state.auth_disabled = False
        response = client.post("/v1/me/mfa/enroll")
        assert response.status_code == 401

    def test_returns_the_enroll_shape(self, client):
        client.app.state.mfa = FakeMfa()

        response = client.post("/v1/me/mfa/enroll", headers=AUTH)

        assert response.status_code == 200
        body = response.json()
        assert body["factor_id"] == "factor-1"
        assert body["secret"] == "BASE32SECRET"
        assert body["qr_uri"] == "otpauth://totp/x"

    def test_recovery_codes_are_empty_pending_a_scope_decision(self, client):
        # Supabase Auth's MFA API has no recovery-code concept (see mfa.py's
        # docstring) — until that's decided, the frozen response field is
        # `[]`, not fabricated data.
        client.app.state.mfa = FakeMfa()

        response = client.post("/v1/me/mfa/enroll", headers=AUTH)

        assert response.json()["recovery_codes"] == []

    def test_forwards_the_caller_s_own_token_not_the_service_key(self, client):
        fake = FakeMfa()
        client.app.state.mfa = fake

        client.post("/v1/me/mfa/enroll", headers=AUTH)

        assert fake.enroll_calls == ["test-user-token"]

    def test_not_configured_is_503(self, client):
        fake = FakeMfa()
        fake.configured = False
        client.app.state.mfa = fake

        response = client.post("/v1/me/mfa/enroll", headers=AUTH)
        assert response.status_code == 503


class FakeFactorLookup:
    def __init__(self):
        self.invalidated: list[str] = []

    def invalidate(self, user_id):
        self.invalidated.append(user_id)


class TestVerifyRoute:
    def test_activates_the_factor(self, client):
        client.app.state.mfa = FakeMfa()

        response = client.post(
            "/v1/me/mfa/verify",
            headers=AUTH,
            json={"factor_id": "factor-1", "code": "123456"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["enrolled"] is True
        assert body["verified_at"] is not None

    def test_invalidates_the_gate_s_factor_cache_on_success(self, client):
        client.app.state.mfa = FakeMfa()
        lookup = FakeFactorLookup()
        client.app.state.mfa_factor_lookup = lookup

        client.post(
            "/v1/me/mfa/verify",
            headers=AUTH,
            json={"factor_id": "factor-1", "code": "123456"},
        )

        assert lookup.invalidated == ["00000000-0000-0000-0000-000000000000"]

    def test_does_not_invalidate_on_a_failed_verify(self, client):
        client.app.state.mfa = FakeMfa(verify_error=InvalidCode("bad code"))
        lookup = FakeFactorLookup()
        client.app.state.mfa_factor_lookup = lookup

        client.post(
            "/v1/me/mfa/verify",
            headers=AUTH,
            json={"factor_id": "factor-1", "code": "000000"},
        )

        assert lookup.invalidated == []

    def test_wrong_code_is_422(self, client):
        client.app.state.mfa = FakeMfa(verify_error=InvalidCode("bad code"))

        response = client.post(
            "/v1/me/mfa/verify",
            headers=AUTH,
            json={"factor_id": "factor-1", "code": "000000"},
        )

        assert response.status_code == 422
        assert response.json()["type"].endswith("invalid-request")

    def test_supabase_failure_is_503_not_422(self, client):
        client.app.state.mfa = FakeMfa(verify_error=MfaError("unreachable"))

        response = client.post(
            "/v1/me/mfa/verify",
            headers=AUTH,
            json={"factor_id": "factor-1", "code": "123456"},
        )
        assert response.status_code == 503


class TestStatusRoute:
    def test_not_enrolled(self, client):
        client.app.state.mfa = FakeMfa(
            status_result=FactorStatus(enrolled=False, verified_at=None)
        )

        response = client.get("/v1/me/mfa", headers=AUTH)

        assert response.status_code == 200
        assert response.json() == {"enrolled": False, "verified_at": None}

    def test_enrolled(self, client):
        client.app.state.mfa = FakeMfa(
            status_result=FactorStatus(
                enrolled=True, verified_at="2026-09-14T00:00:00Z"
            )
        )

        response = client.get("/v1/me/mfa", headers=AUTH)

        assert response.status_code == 200
        body = response.json()
        assert body["enrolled"] is True
        assert body["verified_at"].startswith("2026-09-14")

    def test_supabase_failure_is_503(self, client):
        fake = FakeMfa()

        def boom(user_token):
            raise MfaError("unreachable")

        fake.status = boom
        client.app.state.mfa = fake

        response = client.get("/v1/me/mfa", headers=AUTH)
        assert response.status_code == 503
