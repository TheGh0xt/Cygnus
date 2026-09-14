import pytest

from src.api.mfa import (
    EnrollResult,
    FactorLookup,
    FactorStatus,
    InvalidCode,
    MfaError,
    SupabaseMfa,
    build_factor_lookup,
)


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

    def test_works_for_an_already_verified_factor_not_just_first_enrolment(self):
        # A login-time step-up challenge on a factor verified long ago must
        # succeed the same way a first-enrolment confirmation does —
        # verify() takes no "is this the first time" input, so there is
        # nothing that could make it behave differently for the two cases.
        mfa = FakeMfa()
        mfa.responses = [
            FakeResponse(200, {"id": "challenge-2"}),
            FakeResponse(200, {"access_token": "stepped-up-session-token"}),
        ]

        mfa.verify("user-token", "already-verified-factor", "654321")

        assert mfa.calls[0][:3] == (
            "POST",
            "/factors/already-verified-factor/challenge",
            "user-token",
        )

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


class FakeFactorLookup(FactorLookup):
    def __init__(self, ttl_seconds: float = 60, clock=None):
        super().__init__(
            base_url="https://example.test",
            service_key="service-key",
            ttl_seconds=ttl_seconds,
        )
        self.fetch_calls: list[str] = []
        self.fetch_results: list[object] = []
        self._clock = clock or (lambda: 0.0)

    def _now(self) -> float:
        return self._clock()

    def _fetch(self, user_id: str) -> bool:
        self.fetch_calls.append(user_id)
        result = self.fetch_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class TestFactorLookup:
    def test_not_configured_raises(self):
        lookup = FactorLookup(base_url="", service_key="")
        with pytest.raises(MfaError, match="not configured"):
            lookup.has_verified_totp_factor("user-1")

    def test_true_when_a_verified_totp_factor_exists(self):
        lookup = FakeFactorLookup()
        lookup.fetch_results = [True]
        assert lookup.has_verified_totp_factor("user-1") is True

    def test_false_when_no_verified_factor(self):
        lookup = FakeFactorLookup()
        lookup.fetch_results = [False]
        assert lookup.has_verified_totp_factor("user-1") is False

    def test_caches_within_the_ttl(self):
        lookup = FakeFactorLookup(ttl_seconds=60)
        lookup.fetch_results = [True]
        assert lookup.has_verified_totp_factor("user-1") is True
        assert lookup.has_verified_totp_factor("user-1") is True
        assert lookup.fetch_calls == ["user-1"]

    def test_refetches_after_the_ttl_expires(self):
        clock = {"t": 0.0}
        lookup = FakeFactorLookup(ttl_seconds=60, clock=lambda: clock["t"])
        lookup.fetch_results = [True, False]

        assert lookup.has_verified_totp_factor("user-1") is True
        clock["t"] = 61.0
        assert lookup.has_verified_totp_factor("user-1") is False
        assert lookup.fetch_calls == ["user-1", "user-1"]

    def test_a_failed_lookup_raises_rather_than_defaulting_to_false(self):
        """Fail closed: an outage must not silently look like "not enrolled"."""
        lookup = FakeFactorLookup()
        lookup.fetch_results = [MfaError("unreachable")]
        with pytest.raises(MfaError):
            lookup.has_verified_totp_factor("user-1")

    def test_invalidate_forces_a_fresh_fetch(self):
        lookup = FakeFactorLookup()
        lookup.fetch_results = [False, True]

        assert lookup.has_verified_totp_factor("user-1") is False
        lookup.invalidate("user-1")
        assert lookup.has_verified_totp_factor("user-1") is True
        assert lookup.fetch_calls == ["user-1", "user-1"]

    def test_caches_are_independent_per_user(self):
        lookup = FakeFactorLookup()
        lookup.fetch_results = [True, False]

        assert lookup.has_verified_totp_factor("user-1") is True
        assert lookup.has_verified_totp_factor("user-2") is False
        assert lookup.fetch_calls == ["user-1", "user-2"]


class TestBuildFactorLookup:
    """B.7 review: an unconfigured lookup must not be a silent no-op in

    production — the same failure shape B.13 was built against for the
    memory store (see src/memory/__init__.py's build_memory_store).
    """

    def test_unconfigured_in_production_raises(self, monkeypatch):
        monkeypatch.setenv("PMIE_ENVIRONMENT", "production")
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)

        with pytest.raises(MfaError, match="production"):
            build_factor_lookup()

    def test_configured_in_production_succeeds(self, monkeypatch):
        monkeypatch.setenv("PMIE_ENVIRONMENT", "production")
        monkeypatch.setenv("SUPABASE_URL", "https://example.test")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")

        lookup = build_factor_lookup()

        assert lookup.configured is True

    def test_unconfigured_outside_production_is_a_no_op(self, monkeypatch):
        # Sanity check: the production gate must not change behaviour
        # anywhere else — most local/dev/CI environments have no Supabase
        # project to check MFA against at all.
        monkeypatch.delenv("PMIE_ENVIRONMENT", raising=False)
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)

        lookup = build_factor_lookup()

        assert lookup.configured is False
