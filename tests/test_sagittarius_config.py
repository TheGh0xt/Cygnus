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
    mcp_http_client,
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


class TestMcpHttpClient:
    """The httpx.AsyncClient builder shared by Cygnus's raw `mcp` SDK clients
    — the evaluation worker and generation discovery — which take
    http_client rather than headers directly. sagittarius_connection_params
    (ADK's own toolsets) is covered separately above.

    An async context manager, not a plain function: streamable_http_client
    only manages the lifecycle of a client it creates itself — a client we
    hand it is ours to close, and the caller does that by entering this as
    `async with`. Everything below exercises it that way.
    """

    async def test_still_builds_a_client_when_there_are_no_headers(self):
        # This used to yield None and let the SDK build its own default, which
        # also meant inheriting the SDK's 30s timeout. "No bearer token
        # configured" then silently also meant "cannot survive a cold start".
        # The timeout matters whether or not auth does.
        async with mcp_http_client({}) as client:
            assert client is not None

    async def test_client_carries_the_bearer_header(self):
        async with mcp_http_client({"Authorization": "Bearer secret-token"}) as client:
            assert client is not None
            assert client.headers["authorization"] == "Bearer secret-token"

    async def test_defaults_to_reading_mcp_bearer_token(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER_TOKEN", "secret-token")
        async with mcp_http_client() as client:
            assert client is not None
            assert client.headers["authorization"] == "Bearer secret-token"

    async def test_sends_no_auth_header_when_unset(self, monkeypatch):
        monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
        async with mcp_http_client() as client:
            assert client is not None
            assert "authorization" not in client.headers

    async def test_outlives_a_free_plan_cold_start(self, monkeypatch):
        """The connect budget must clear Sagittarius waking up, not merely
        beat httpx's 5s default.

        The previous version of this test compared against a fresh
        create_mcp_http_client() and asserted equality — which pinned the SDK's
        MCP_DEFAULT_TIMEOUT of **30s**, while its own comment said Sagittarius
        "takes ~50s to wake on the free plan". The reasoning was right and the
        assertion contradicted it.

        What that cost, on 2026-09-22: the scheduled evaluation cron failed
        with price_unavailable=1 after a 33-second run — 30s of connect timeout
        plus overhead — against a market that was open and priced throughout.
        The same ceiling sits under the personalised feed.

        So this now asserts against SAGITTARIUS_TIMEOUT_SECONDS, the setting
        render.yaml already carries for exactly this reason, rather than
        against whatever the SDK happens to default to.
        """
        from mcp.shared._httpx_utils import MCP_DEFAULT_TIMEOUT

        monkeypatch.setenv("SAGITTARIUS_TIMEOUT_SECONDS", "90")

        async with mcp_http_client({"Authorization": "Bearer t"}) as client:
            assert client.timeout.connect == 90.0
            assert client.timeout.connect > MCP_DEFAULT_TIMEOUT
            # A cold start is ~50s; anything at or under that is a coin flip.
            assert client.timeout.connect > 50.0
            # Streaming still gets its own, much longer budget — the read
            # timeout bounds the gap between chunks, not the whole call.
            assert client.timeout.read == 300.0

    async def test_the_timeout_applies_without_a_token_too(self, monkeypatch):
        monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
        monkeypatch.setenv("SAGITTARIUS_TIMEOUT_SECONDS", "90")

        async with mcp_http_client() as client:
            assert client.timeout.connect == 90.0

    async def test_follows_redirects_like_the_sdk_default(self):
        from mcp.shared._httpx_utils import create_mcp_http_client

        async with create_mcp_http_client() as reference:
            async with mcp_http_client({"Authorization": "Bearer t"}) as client:
                assert client.follow_redirects == reference.follow_redirects

    async def test_closes_the_client_it_created(self):
        # streamable_http_client only closes a client it built itself; one
        # handed in via http_client= is ours to close, or it leaks a
        # connection pool on every call. httpx.AsyncClient.__aexit__ closes
        # the transport directly rather than calling .aclose() (confirmed by
        # reading its source), so is_closed is the true signal here — a
        # mocked .aclose() would pass even with the leak still present.
        async with mcp_http_client({"Authorization": "Bearer secret-token"}) as client:
            assert client.is_closed is False
        assert client.is_closed is True

    async def test_closes_the_client_even_with_no_headers(self):
        # A client is now always created — including when there is no bearer
        # token — so it must always be closed too. This previously yielded
        # None and had nothing to clean up.
        async with mcp_http_client({}) as client:
            assert client is not None
        assert client.is_closed


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
