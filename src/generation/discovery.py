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

from .selector import Candidate

logger = logging.getLogger("cygnus.generation.discovery")

_TOOL = "get_moving_markets"


class DiscoveryError(Exception):
    """Sagittarius could not be reached, or its reply could not be read."""


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

    return Candidate(
        market_slug=slug,
        title=raw.get("title") or slug,
        category=raw.get("category") or "",
        probability=_as_float(raw.get("probability")),
        change_24h=_as_float(raw.get("change_24h")),
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

    def __init__(self, mcp_url: str, timeout: float | None = None):
        self.mcp_url = mcp_url
        self._timeout = timeout

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
                streamable_http_client(self.mcp_url) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await session.call_tool(_TOOL, args)
        except Exception as exc:
            # Wrapped, not swallowed. The caller turns this into a typed skip
            # reason that reaches the cron log.
            raise DiscoveryError(
                f"could not reach Sagittarius at {self.mcp_url}: {exc}"
            ) from exc

        return parse_moving_markets(result)
