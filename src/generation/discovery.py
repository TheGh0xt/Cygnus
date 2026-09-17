"""Asking Sagittarius what is moving.

Deliberately noisier than `SagittariusPriceFetcher`, which catches every
exception and returns None. That silence is what made 2026-08-20 expensive:
a broken fetch and a quiet market produced identical output, so three separate
documents recorded the wrong root cause and nobody could tell from outside.

Here a failure raises `DiscoveryError` with the reason attached, and the cycle
records it as a typed skip. The degradation is still graceful — one bad cycle
costs a delay, not data — but it is never invisible.
"""

from __future__ import annotations

import json
import logging

import httpx

from ..config import mcp_auth_headers, mcp_http_client
from .selector import Candidate

logger = logging.getLogger("cygnus.generation.discovery")

_TOOL = "get_moving_markets"

# Headers that tell an HTTP 429 apart from an app-level one: cf-ray present
# with no Render origin header means Cloudflare answered and the request
# never reached our container (F5) — the other two are corroborating.
_DIAGNOSTIC_HEADERS = ("retry-after", "cf-ray", "server", "x-render-origin-server")
_BODY_SNIPPET_LIMIT = 200


class DiscoveryError(Exception):
    """Sagittarius could not be reached, or its reply could not be read."""


def _unwrap_exception_group(exc: BaseException) -> BaseException:
    """Return the first real failure inside an ExceptionGroup, recursively.

    anyio's TaskGroup — used internally by ClientSession and
    streamable_http_client — wraps every failure in an ExceptionGroup whose
    own str() is the unhelpful "unhandled errors in a TaskGroup
    (1 sub-exception)". That string is what reached the cron log on every one
    of 39 failed production runs (F5), hiding whether the real cause was a
    403 from a misconfigured proxy, a timeout, or something else entirely.
    """
    while isinstance(exc, ExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _sanitize(value: str) -> str:
    """Strip characters that would break the workflow's discovery_error sed.

    The capture is `[^"]*` and assumes one line — a `"` in a header value or
    response body would truncate it early, and a newline would smuggle a
    second line into a log statement that assumes one.
    """
    return value.replace('"', "'").replace("\n", " ").replace("\r", " ")


def _http_status_detail(real: BaseException) -> str:
    """Evidence an httpx.HTTPStatusError carries that str(exc) discards.

    None of status, retry-after, cf-ray, server, or the render origin header
    survive str(exc) — without them a 429 from Cloudflare's edge and a 429
    from Sagittarius itself (which its /mcp handler never actually sends) are
    indistinguishable in the log (F5).
    """
    if not isinstance(real, httpx.HTTPStatusError):
        return ""

    response = real.response
    parts = [f"status={response.status_code}"]
    for name in _DIAGNOSTIC_HEADERS:
        value = response.headers.get(name)
        if value:
            parts.append(f"{name}={_sanitize(value)}")

    try:
        body = _sanitize(response.text[:_BODY_SNIPPET_LIMIT])
    except httpx.ResponseNotRead:
        # The body may not be readable (e.g. an unread stream) — the headers
        # above already carry the decisive signal.
        body = ""
    if body:
        # Not repr(): if the sanitized body still contains a "'" (originally
        # a '"'), repr() would switch to double quotes to avoid escaping it,
        # reintroducing the character _sanitize just removed.
        parts.append(f"body='{body}'")

    return " [" + ", ".join(parts) + "]"


def _count_leaves(exc: BaseException) -> int:
    """Total non-group exceptions inside `exc`, flattened recursively.

    Backs the "(+N more)" suffix: a TaskGroup can fail more than one task at
    once, and reporting only the first leaf without a trace of the rest
    would silently drop that.
    """
    if isinstance(exc, ExceptionGroup):
        if not exc.exceptions:
            return 0
        return sum(_count_leaves(sub) for sub in exc.exceptions)
    return 1


def parse_moving_markets(result: object) -> list[Candidate]:
    """Turn a get_moving_markets tool result into candidates.

    Separated from the network call so the decode — where a tool-contract
    change would otherwise show up as a silently empty feed — is directly
    testable.
    """
    from mcp.types import CallToolResult, TextContent

    if not isinstance(result, CallToolResult):
        raise DiscoveryError(f"unexpected result type: {type(result).__name__}")
    if result.isError:
        raise DiscoveryError(f"{_TOOL} returned an error: {_text_of(result)!r}")

    block = next((b for b in result.content if isinstance(b, TextContent)), None)
    if block is None:
        raise DiscoveryError(f"{_TOOL} returned no text content")

    try:
        payload = json.loads(block.text)
    except json.JSONDecodeError as exc:
        raise DiscoveryError(f"{_TOOL} returned invalid JSON: {exc}") from exc

    if not isinstance(payload, dict) or "markets" not in payload:
        raise DiscoveryError(
            f"{_TOOL} reply has no 'markets' key; the tool contract may have changed"
        )

    markets = payload["markets"]
    if markets is None:
        return []
    if not isinstance(markets, list):
        raise DiscoveryError(f"{_TOOL} returned a non-list 'markets'")

    candidates: list[Candidate] = []
    for raw in markets:
        candidate = _candidate_from(raw)
        if candidate is None:
            # One malformed row must not cost the whole cycle — the lesson the
            # Gamma timestamp bug taught on the Sagittarius side, where a
            # single unparseable field emptied three categories.
            logger.warning("skipping unreadable market in %s reply: %r", _TOOL, raw)
            continue
        candidates.append(candidate)

    return candidates


def _candidate_from(raw: object) -> Candidate | None:
    if not isinstance(raw, dict):
        return None
    slug = raw.get("slug")
    if not isinstance(slug, str) or not slug:
        return None

    end_date = raw.get("end_date")
    return Candidate(
        market_slug=slug,
        title=raw.get("title") or slug,
        category=raw.get("category") or "",
        probability=_as_float(raw.get("probability")),
        change_24h=_as_float(raw.get("change_24h")),
        volume_24h=_as_float(raw.get("volume_24h")),
        end_date=end_date if isinstance(end_date, str) and end_date else None,
    )


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def _text_of(result: object) -> str:
    from mcp.types import TextContent

    block = next(
        (b for b in getattr(result, "content", []) if isinstance(b, TextContent)), None
    )
    return block.text[:200] if block else ""


class SagittariusDiscovery:
    """Fetches moving markets over MCP.

    Network shim, verified end to end rather than by unit test; the decode it
    delegates to is unit-tested above.
    """

    def __init__(
        self,
        mcp_url: str,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.mcp_url = mcp_url
        self._timeout = timeout
        # B.3: the same bearer token Sagittarius enforces on /mcp. This MCP
        # client is separate from the ADK agent toolsets (src/config.py) and
        # the evaluation worker's own client, so it needs the header wired in
        # independently. Defaults to reading MCP_BEARER_TOKEN itself so the
        # one production call site (app.py) doesn't have to remember to pass
        # it.
        self._headers = mcp_auth_headers() if headers is None else headers

    async def moving_markets(
        self, categories: list[str] | None = None, limit: int = 20
    ) -> list[Candidate]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        args: dict[str, object] = {"limit": limit}
        if categories:
            args["categories"] = categories

        try:
            async with (
                mcp_http_client(self._headers) as http_client,
                streamable_http_client(self.mcp_url, http_client=http_client) as (
                    read,
                    write,
                    _,
                ),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await session.call_tool(_TOOL, args)
        except Exception as exc:
            # Wrapped, not swallowed. The caller turns this into a typed skip
            # reason that reaches the cron log. Unwrapped first (F5): anyio's
            # TaskGroup wraps every failure from the async with above in an
            # ExceptionGroup whose own message is the useless "unhandled
            # errors in a TaskGroup (1 sub-exception)" — every one of 39
            # failed production runs logged exactly that, with the real
            # cause hidden inside it.
            real = _unwrap_exception_group(exc)
            # The type name is load-bearing, not decoration: httpx's
            # ReadTimeout/ConnectTimeout and anyio's ClosedResourceError/
            # EndOfStream commonly stringify to "" — without the type, the
            # log would read "...: " with nothing after it, no better than
            # the wrapper text this replaces.
            remaining = _count_leaves(exc) - 1
            suffix = f" (+{remaining} more)" if remaining > 0 else ""
            detail = _http_status_detail(real)
            raise DiscoveryError(
                f"could not reach Sagittarius at {self.mcp_url}: "
                f"{type(real).__name__}: {real}{suffix}{detail}"
            ) from exc

        return parse_moving_markets(result)
