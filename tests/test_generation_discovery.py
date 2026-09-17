"""Turning Sagittarius's get_moving_markets reply into candidates.

The parsing is tested directly rather than through a live MCP session: the
network shim is exercised by end-to-end verification, but the decode is where
a schema change would silently produce an empty feed, so it gets real tests.
"""

import json

import pytest

from src.generation import discovery as discovery_module
from src.generation.discovery import (
    DiscoveryError,
    SagittariusDiscovery,
    _count_leaves,
    _unwrap_exception_group,
    parse_moving_markets,
)


def _tool_result(payload, is_error: bool = False):
    from mcp.types import CallToolResult, TextContent

    text = payload if isinstance(payload, str) else json.dumps(payload)
    return CallToolResult(
        content=[TextContent(type="text", text=text)], isError=is_error
    )


class TestParsing:
    def test_reads_markets_into_candidates(self):
        result = _tool_result(
            {
                "categories": ["Politics"],
                "markets": [
                    {
                        "slug": "who-will-bernie-endorse",
                        "title": "Who will Bernie endorse?",
                        "category": "Politics",
                        "probability": 0.275,
                        "change_24h": 0.336,
                        "volume_24h": 1000.0,
                        "end_date": "2026-11-04T00:00:00Z",
                    }
                ],
            }
        )

        candidates = parse_moving_markets(result)

        assert len(candidates) == 1
        assert candidates[0].market_slug == "who-will-bernie-endorse"
        assert candidates[0].category == "Politics"
        assert candidates[0].change_24h == 0.336
        assert candidates[0].volume_24h == 1000.0
        assert candidates[0].end_date == "2026-11-04T00:00:00Z"

    def test_missing_volume_and_end_date_default_safely(self):
        # The scheduled generation cycle never reads these two fields, so a
        # reply shaped for it (no volume_24h/end_date) must not fail to
        # parse — only the feed (B.17) needs them.
        result = _tool_result(
            {
                "markets": [
                    {
                        "slug": "no-extras",
                        "title": "fine",
                        "category": "Crypto",
                        "probability": 0.4,
                        "change_24h": 0.1,
                    }
                ]
            }
        )

        candidates = parse_moving_markets(result)

        assert candidates[0].volume_24h == 0.0
        assert candidates[0].end_date is None

    def test_empty_market_list_is_not_an_error(self):
        # A quiet period is a legitimate answer, distinct from a broken tool.
        assert parse_moving_markets(_tool_result({"markets": []})) == []

    def test_a_market_missing_its_slug_is_dropped_not_fatal(self):
        # One malformed row must not cost the whole cycle — the same rule the
        # Gamma timestamp bug taught on the Go side.
        result = _tool_result(
            {
                "markets": [
                    {"title": "no slug here", "probability": 0.4, "change_24h": 0.1},
                    {
                        "slug": "good-one",
                        "title": "fine",
                        "category": "Crypto",
                        "probability": 0.4,
                        "change_24h": 0.1,
                    },
                ]
            }
        )

        candidates = parse_moving_markets(result)

        assert [c.market_slug for c in candidates] == ["good-one"]


class TestFailuresAreLoud:
    """The price fetcher swallowed every failure into None and cost five days
    of misdiagnosis. This client raises a typed error instead."""

    def test_tool_error_raises(self):
        with pytest.raises(DiscoveryError):
            parse_moving_markets(_tool_result({"markets": []}, is_error=True))

    def test_non_json_reply_raises(self):
        with pytest.raises(DiscoveryError):
            parse_moving_markets(_tool_result("this is not json"))

    def test_missing_markets_key_raises(self):
        # Distinct from an empty list: the shape is wrong, which means the
        # tool contract changed and somebody needs to know.
        with pytest.raises(DiscoveryError):
            parse_moving_markets(_tool_result({"categories": ["Politics"]}))

    def test_empty_content_raises(self):
        from mcp.types import CallToolResult

        with pytest.raises(DiscoveryError):
            parse_moving_markets(CallToolResult(content=[], isError=False))


class TestUnwrapExceptionGroup:
    """F5: production generation has failed on every one of 39 runs since
    09-07 with the same useless message — "unhandled errors in a TaskGroup
    (1 sub-exception)" — because anyio's TaskGroup (used internally by
    ClientSession/streamable_http_client) wraps every failure in an
    ExceptionGroup whose own str() hides the real cause."""

    def test_plain_exception_is_returned_unchanged(self):
        exc = ConnectionRefusedError("refused")
        assert _unwrap_exception_group(exc) is exc

    def test_unwraps_a_single_level_group(self):
        real = ConnectionRefusedError("[Errno 61] Connection refused")
        group = ExceptionGroup("unhandled errors in a TaskGroup", [real])
        assert _unwrap_exception_group(group) is real

    def test_unwraps_nested_groups(self):
        real = TimeoutError("timed out")
        inner = ExceptionGroup("inner", [real])
        outer = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
        assert _unwrap_exception_group(outer) is real


class TestCountLeaves:
    """Backs the optional "(+N more)" suffix — a group with several failed
    tasks must not silently drop every leaf but the first."""

    def test_a_plain_exception_counts_as_one(self):
        assert _count_leaves(ValueError("x")) == 1

    def test_flat_group_counts_every_leaf(self):
        group = ExceptionGroup("g", [ValueError("a"), TypeError("b")])
        assert _count_leaves(group) == 2

    def test_nested_groups_count_recursively(self):
        inner = ExceptionGroup("inner", [ValueError("a"), TypeError("b")])
        outer = ExceptionGroup("outer", [inner, KeyError("c")])
        assert _count_leaves(outer) == 3


class _RaisingAsyncCM:
    """A stand-in for mcp_http_client that raises on entry, so moving_markets
    exercises its real except-and-wrap path without a live MCP session."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *args):
        return False


class TestDiscoveryErrorUnwrapsExceptionGroups:
    async def test_reports_the_real_cause_not_the_taskgroup_wrapper(self, monkeypatch):
        real = ConnectionRefusedError("[Errno 61] Connection refused")
        group = ExceptionGroup("unhandled errors in a TaskGroup", [real])
        monkeypatch.setattr(
            discovery_module,
            "mcp_http_client",
            lambda headers: _RaisingAsyncCM(group),
        )

        discovery = SagittariusDiscovery("http://localhost:8080/mcp")
        with pytest.raises(DiscoveryError) as exc_info:
            await discovery.moving_markets()

        message = str(exc_info.value)
        assert "Connection refused" in message
        assert "unhandled errors in a TaskGroup" not in message

    async def test_a_plain_exception_still_reports_its_own_message(self, monkeypatch):
        # Not every failure is wrapped in a TaskGroup — a plain exception
        # (e.g. a DNS failure raised directly) must pass through unchanged.
        monkeypatch.setattr(
            discovery_module,
            "mcp_http_client",
            lambda headers: _RaisingAsyncCM(OSError("nodename nor servname")),
        )

        discovery = SagittariusDiscovery("http://localhost:8080/mcp")
        with pytest.raises(DiscoveryError, match="nodename nor servname"):
            await discovery.moving_markets()

    async def test_empty_message_leaf_still_names_its_type(self, monkeypatch):
        # httpx's ReadTimeout/ConnectTimeout and anyio's ClosedResourceError/
        # EndOfStream commonly stringify to "" — without the type name the
        # cron log would read "...: " with nothing after it, no better off
        # than the TaskGroup wrapper text this PR replaces.
        group = ExceptionGroup("unhandled errors in a TaskGroup", [TimeoutError()])
        monkeypatch.setattr(
            discovery_module,
            "mcp_http_client",
            lambda headers: _RaisingAsyncCM(group),
        )

        discovery = SagittariusDiscovery("http://localhost:8080/mcp")
        with pytest.raises(DiscoveryError, match="TimeoutError"):
            await discovery.moving_markets()

    async def test_several_leaves_reports_how_many_more(self, monkeypatch):
        # A TaskGroup can fail more than one task at once; dropping every
        # leaf but the first without a trace would hide that.
        group = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [ConnectionRefusedError("refused"), TimeoutError("timed out")],
        )
        monkeypatch.setattr(
            discovery_module,
            "mcp_http_client",
            lambda headers: _RaisingAsyncCM(group),
        )

        discovery = SagittariusDiscovery("http://localhost:8080/mcp")
        with pytest.raises(DiscoveryError, match=r"\(\+1 more\)"):
            await discovery.moving_markets()


class TestHttpStatusErrorDetail:
    """F5, part two: the unwrap named the type (HTTPStatusError) but threw
    away everything that would say *who* returned the 429 — Sagittarius's own
    handler never answers 429 (grep confirms; it only answers 401, 403, or
    MCP), so the edge in front of it is the leading suspect. `cf-ray` present
    with no Render origin header is what would confirm that without paying
    for the upgrade first."""

    @staticmethod
    def _http_status_error(
        status_code: int = 429, headers: dict[str, str] | None = None, text: str = ""
    ):
        import httpx

        request = httpx.Request("POST", "https://sagittarius-rp3z.onrender.com/mcp")
        response = httpx.Response(
            status_code, headers=headers or {}, text=text, request=request
        )
        return httpx.HTTPStatusError(
            f"{status_code} error", request=request, response=response
        )

    async def test_reports_status_and_cloudflare_headers_with_no_origin_header(
        self, monkeypatch
    ):
        # The decisive signal: cf-ray present, x-render-origin-server absent.
        error = self._http_status_error(
            headers={
                "retry-after": "5",
                "cf-ray": "8f1a2b3c4d5e6f70-SJC",
                "server": "cloudflare",
            },
            text="rate limited",
        )
        monkeypatch.setattr(
            discovery_module,
            "mcp_http_client",
            lambda headers: _RaisingAsyncCM(error),
        )

        discovery = SagittariusDiscovery("http://localhost:8080/mcp")
        with pytest.raises(DiscoveryError) as exc_info:
            await discovery.moving_markets()

        message = str(exc_info.value)
        assert "429" in message
        assert "retry-after=5" in message
        assert "cf-ray=8f1a2b3c4d5e6f70-SJC" in message
        assert "server=cloudflare" in message
        assert "x-render-origin-server" not in message

    async def test_reports_render_origin_header_when_present(self, monkeypatch):
        # The counter-evidence: if Render's own header shows up, the request
        # reached our container and the edge hypothesis is wrong.
        error = self._http_status_error(headers={"x-render-origin-server": "gunicorn"})
        monkeypatch.setattr(
            discovery_module,
            "mcp_http_client",
            lambda headers: _RaisingAsyncCM(error),
        )

        discovery = SagittariusDiscovery("http://localhost:8080/mcp")
        with pytest.raises(DiscoveryError, match="x-render-origin-server=gunicorn"):
            await discovery.moving_markets()

    async def test_body_snippet_is_included_and_capped(self, monkeypatch):
        error = self._http_status_error(text="x" * 500)
        monkeypatch.setattr(
            discovery_module,
            "mcp_http_client",
            lambda headers: _RaisingAsyncCM(error),
        )

        discovery = SagittariusDiscovery("http://localhost:8080/mcp")
        with pytest.raises(DiscoveryError) as exc_info:
            await discovery.moving_markets()

        message = str(exc_info.value)
        assert "x" * 200 in message
        assert "x" * 201 not in message

    async def test_message_stays_single_line_with_no_embedded_quotes(self, monkeypatch):
        # The workflow greps discovery_error with a sed pattern that stops at
        # the first literal '"' and assumes one line — a body snippet or
        # header value must not smuggle either in and break that capture.
        error = self._http_status_error(
            headers={"server": 'cloud"flare'}, text='{"error":\n"too many requests"}'
        )
        monkeypatch.setattr(
            discovery_module,
            "mcp_http_client",
            lambda headers: _RaisingAsyncCM(error),
        )

        discovery = SagittariusDiscovery("http://localhost:8080/mcp")
        with pytest.raises(DiscoveryError) as exc_info:
            await discovery.moving_markets()

        message = str(exc_info.value)
        assert '"' not in message
        assert "\n" not in message

    async def test_non_http_status_error_is_unaffected(self, monkeypatch):
        # Scope check: a plain timeout must not gain the new suffix at all.
        monkeypatch.setattr(
            discovery_module,
            "mcp_http_client",
            lambda headers: _RaisingAsyncCM(TimeoutError("timed out")),
        )

        discovery = SagittariusDiscovery("http://localhost:8080/mcp")
        with pytest.raises(DiscoveryError) as exc_info:
            await discovery.moving_markets()

        message = str(exc_info.value)
        assert message == (
            "could not reach Sagittarius at http://localhost:8080/mcp: "
            "TimeoutError: timed out"
        )


class TestAuth:
    """B.3, client side: this is the third of Cygnus's three MCP clients to
    Sagittarius — the ADK agent toolsets and the evaluation worker's own
    client are the other two — and needs the bearer token wired in
    independently of both."""

    def test_defaults_to_reading_mcp_bearer_token(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER_TOKEN", "secret-token")
        discovery = SagittariusDiscovery("http://localhost:8080/mcp")
        assert discovery._headers == {"Authorization": "Bearer secret-token"}

    def test_defaults_to_no_headers_when_unset(self, monkeypatch):
        monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
        discovery = SagittariusDiscovery("http://localhost:8080/mcp")
        assert discovery._headers == {}

    def test_explicit_headers_override_the_environment(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER_TOKEN", "env-token")
        discovery = SagittariusDiscovery(
            "http://localhost:8080/mcp",
            headers={"Authorization": "Bearer explicit-token"},
        )
        assert discovery._headers == {"Authorization": "Bearer explicit-token"}
