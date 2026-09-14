"""The Sagittarius connection timeout.

A five-second default cost a whole deployed analysis. Sagittarius sleeps on a
free container plan and takes roughly fifty seconds to wake, so the event and
signal stages both timed out, the news stage succeeded because it uses Google
Search, and the analyst produced a confident EXTERNAL_NEWS report about
football transfers with no market data in it whatsoever. The run reported
success.

These pin the timeout so nobody re-inherits the default by accident.
"""

import pytest

from src.config import (
    mcp_auth_headers,
    sagittarius_connection_params,
    sagittarius_timeout,
    sagittarius_url,
    warm_sagittarius,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("SAGITTARIUS_MCP_URL", raising=False)
    monkeypatch.delenv("SAGITTARIUS_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)


class TestTimeout:
    def test_default_outlasts_a_cold_start(self):
        # A free-tier container takes ~50s to wake. Anything near the ADK's
        # 5s default silently loses every Sagittarius-backed stage.
        assert sagittarius_timeout() >= 60

    def test_is_configurable(self, monkeypatch):
        monkeypatch.setenv("SAGITTARIUS_TIMEOUT_SECONDS", "120")
        assert sagittarius_timeout() == 120.0

    def test_nonsense_value_falls_back_rather_than_crashing(self, monkeypatch):
        monkeypatch.setenv("SAGITTARIUS_TIMEOUT_SECONDS", "soon")
        assert sagittarius_timeout() >= 60


class TestConnectionParams:
    def test_params_carry_the_timeout(self):
        # The actual regression: ADK's default is 5.0, and nothing in the
        # agent definitions made that visible.
        params = sagittarius_connection_params()
        assert params.timeout >= 60
        assert params.timeout != 5.0

    def test_params_carry_the_url(self, monkeypatch):
        monkeypatch.setenv("SAGITTARIUS_MCP_URL", "https://sag.example/mcp")
        assert sagittarius_connection_params().url == "https://sag.example/mcp"


class TestUrl:
    def test_defaults_to_localhost(self):
        assert sagittarius_url() == "http://localhost:8080/mcp"


class TestMcpAuthHeaders:
    """B.3, client side: Cygnus must send the same bearer token Sagittarius
    enforces on /mcp — same env var name and value, MCP_BEARER_TOKEN."""

    def test_unset_produces_no_headers(self):
        # Behaviour must be unchanged while the token is unset: Sagittarius
        # isn't enforcing yet, and B.3's own rollout note says this side must
        # deploy before Sagittarius turns enforcement on.
        assert mcp_auth_headers() == {}

    def test_set_produces_the_bearer_header(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER_TOKEN", "secret-token")
        assert mcp_auth_headers() == {"Authorization": "Bearer secret-token"}

    def test_empty_string_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER_TOKEN", "")
        assert mcp_auth_headers() == {}


class TestConnectionParamsCarryAuth:
    def test_no_headers_when_token_unset(self):
        assert sagittarius_connection_params().headers is None

    def test_headers_carry_the_bearer_token(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER_TOKEN", "secret-token")
        params = sagittarius_connection_params()
        assert params.headers == {"Authorization": "Bearer secret-token"}


class TestWarmUp:
    def test_derives_the_health_url_from_the_mcp_url(self, monkeypatch):
        called = {}

        def fake_get(url, timeout=None):
            called["url"] = url

            class R:
                status_code = 200

            return R()

        monkeypatch.setenv("SAGITTARIUS_MCP_URL", "https://sag.example/mcp")
        monkeypatch.setattr("httpx.get", fake_get)

        assert warm_sagittarius() is True
        assert called["url"] == "https://sag.example/health"

    def test_never_raises_when_sagittarius_is_down(self, monkeypatch):
        # Startup must not fail because a dependency is asleep.
        def boom(url, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr("httpx.get", boom)
        assert warm_sagittarius() is False
