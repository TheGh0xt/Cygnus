import pytest

from src.api.mfa import EnrollResult, FactorStatus, InvalidCode, MfaError, SupabaseMfa


class FakeResponse:
    def __init__(self, status_code: int, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text or str(self._json)

    def json(self):
        return self._json


class FakeMfa(SupabaseMfa):
    def __init__(self):
        super().__init__(base_url="https://example.test", service_key="service-key")
        self.calls: list[tuple[str, str, str, dict]] = []
        self.responses: list[FakeResponse] = []

    def _request(self, method, path, user_token, **kwargs):
        self.calls.append((method, path, user_token, kwargs))
        return self.responses.pop(0)


class TestEnroll:
    def test_returns_factor_id_secret_and_qr_uri(self):
        mfa = FakeMfa()
        mfa.responses = [
            FakeResponse(
                200,
                {
                    "id": "factor-1",
                    "totp": {"secret": "BASE32SECRET", "uri": "otpauth://totp/x"},
                },
            )
        ]

        result = mfa.enroll("user-token")

        assert result == EnrollResult(
            factor_id="factor-1", secret="BASE32SECRET", qr_uri="otpauth://totp/x"
        )
        method, path, token, kwargs = mfa.calls[0]
        assert (method, path, token) == ("POST", "/factors", "user-token")
        assert kwargs["json"]["factor_type"] == "totp"

    def test_raises_on_error_response(self):
        mfa = FakeMfa()
        mfa.responses = [FakeResponse(500, text="boom")]
        with pytest.raises(MfaError):
            mfa.enroll("user-token")

    def test_raises_on_malformed_reply(self):
        mfa = FakeMfa()
        mfa.responses = [FakeResponse(200, {"id": "factor-1"})]  # no totp block
        with pytest.raises(MfaError):
            mfa.enroll("user-token")


class TestVerify:
    def test_challenges_then_verifies(self):
        mfa = FakeMfa()
        mfa.responses = [
            FakeResponse(200, {"id": "challenge-1"}),
            FakeResponse(200, {"access_token": "new-session-token"}),
        ]

        mfa.verify("user-token", "factor-1", "123456")

        challenge_call = mfa.calls[0]
        assert challenge_call[:3] == (
            "POST",
            "/factors/factor-1/challenge",
            "user-token",
        )
        verify_call = mfa.calls[1]
        assert verify_call[:3] == ("POST", "/factors/factor-1/verify", "user-token")
        assert verify_call[3]["json"] == {
            "challenge_id": "challenge-1",
            "code": "123456",
        }

    def test_wrong_code_raises_invalid_code(self):
        mfa = FakeMfa()
        mfa.responses = [
            FakeResponse(200, {"id": "challenge-1"}),
            FakeResponse(422, text="invalid code"),
        ]

        with pytest.raises(InvalidCode):
            mfa.verify("user-token", "factor-1", "000000")

    def test_challenge_failure_is_mfa_error_not_invalid_code(self):
        mfa = FakeMfa()
        mfa.responses = [FakeResponse(500, text="boom")]

        with pytest.raises(MfaError) as exc_info:
            mfa.verify("user-token", "factor-1", "123456")
        assert not isinstance(exc_info.value, InvalidCode)


class TestStatus:
    def test_no_factors_is_not_enrolled(self):
        mfa = FakeMfa()
        mfa.responses = [FakeResponse(200, {"factors": []})]
        assert mfa.status("user-token") == FactorStatus(
            enrolled=False, verified_at=None
        )

    def test_unverified_totp_factor_is_not_enrolled(self):
        mfa = FakeMfa()
        mfa.responses = [
            FakeResponse(
                200,
                {"factors": [{"factor_type": "totp", "status": "unverified"}]},
            )
        ]
        assert mfa.status("user-token").enrolled is False

    def test_verified_totp_factor_is_enrolled(self):
        mfa = FakeMfa()
        mfa.responses = [
            FakeResponse(
                200,
                {
                    "factors": [
                        {
                            "factor_type": "totp",
                            "status": "verified",
                            "updated_at": "2026-09-14T00:00:00Z",
                        }
                    ]
                },
            )
        ]
        status = mfa.status("user-token")
        assert status.enrolled is True
        assert status.verified_at == "2026-09-14T00:00:00Z"


def test_not_configured_raises():
    mfa = SupabaseMfa(base_url="", service_key="")
    with pytest.raises(MfaError, match="not configured"):
        mfa.enroll("user-token")
