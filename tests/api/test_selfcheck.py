"""The startup write check.

Catches the failure where Supabase's publishable key is configured in place
of the secret one: reads keep working, every write is refused, and the server
looks perfectly healthy while discarding every report.
"""

import pytest

from src.api.selfcheck import inspect_key_shape, run_startup_checks, verify_write_access


class FakeAccounts:
    """Accounts with the HTTP layer replaced by a scripted outcome."""

    configured = True

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path))
        if self.error and method == "POST":
            raise self.error

        class R:
            @staticmethod
            def json():
                return []

        return R()


@pytest.fixture(autouse=True)
def secret_key(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_looksright")
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)


class TestKeyShape:
    def test_secret_key_passes(self):
        assert inspect_key_shape().ok

    def test_legacy_jwt_service_role_passes(self, monkeypatch):
        # Older projects have a JWT rather than an sb_secret_ key.
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "eyJhbGciOiJIUzI1NiJ9.aaa.bbb")
        assert inspect_key_shape().ok

    def test_publishable_key_is_named_explicitly(self, monkeypatch):
        # The actual mistake, and the message has to say so — this is the whole
        # point of the check.
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_publishable_abc123")
        result = inspect_key_shape()
        assert not result.ok
        assert "publishable" in result.detail.lower()

    def test_missing_url_is_reported(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        assert not inspect_key_shape().ok

    def test_missing_key_is_reported(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        result = inspect_key_shape()
        assert not result.ok
        assert "secret key" in result.detail.lower()


class TestWriteProbe:
    def test_successful_write_is_cleaned_up(self):
        accounts = FakeAccounts()
        result = verify_write_access(accounts)
        assert result.ok
        methods = [method for method, _ in accounts.calls]
        assert methods == ["POST", "DELETE"], "the probe row must be removed"

    def test_rls_refusal_is_diagnosed(self):
        accounts = FakeAccounts(
            error=Exception(
                'returned 401: {"code":"42501","message":"new row '
                'violates row-level security policy"}'
            )
        )
        result = verify_write_access(accounts)
        assert not result.ok
        assert "publishable" in result.detail.lower()

    def test_foreign_key_rejection_still_counts_as_write_access(self):
        # The probe profile does not exist, so a correctly-authorised insert
        # can be refused on referential grounds. That is a pass: the write was
        # allowed, which is all this check asks.
        accounts = FakeAccounts(
            error=Exception('returned 409: {"code":"23503","message":"foreign key"}')
        )
        assert verify_write_access(accounts).ok

    def test_unconfigured_accounts_fail_the_check(self):
        class Unconfigured:
            configured = False

        assert not verify_write_access(Unconfigured()).ok

    def test_probe_never_raises_into_the_caller(self):
        # Startup must not be taken down by this check.
        accounts = FakeAccounts(error=Exception("connection reset"))
        result = verify_write_access(accounts)
        assert not result.ok


class TestStartupChecks:
    def test_failure_is_logged_at_critical_severity(self, caplog):
        accounts = FakeAccounts(
            error=Exception('{"code":"42501","message":"row-level security"}')
        )
        with caplog.at_level("ERROR"):
            result = run_startup_checks(accounts)
        assert not result.ok
        assert "STARTUP CHECK FAILED" in caplog.text
        assert "cannot persist reports" in caplog.text

    def test_success_is_quiet(self, caplog):
        with caplog.at_level("ERROR"):
            assert run_startup_checks(FakeAccounts()).ok
        assert "STARTUP CHECK FAILED" not in caplog.text
