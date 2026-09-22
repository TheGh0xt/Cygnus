"""How Cygnus reaches Sagittarius.

One place, because getting the timeout wrong here is silent and expensive.

The ADK's StreamableHTTPConnectionParams defaults to a **5 second** connect
timeout. That is fine against a local Sagittarius and catastrophic against a
sleeping one: free container platforms suspend an idle service, and waking it
takes roughly fifty seconds.

What that produced in production: the event and signal stages both failed at
five seconds, the news stage succeeded because it uses Google Search and
never touches Sagittarius, and the analyst — reasoning only over the state it
was given, exactly as designed — wrote a confident EXTERNAL_NEWS report about
football transfer gossip with no market data in it at all. The run reported
success. Nothing in the output said the market data was missing.

So the timeout is generous by default and configurable, and Cygnus warms
Sagittarius at startup so the first real analysis is not the request that
pays for the cold start.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import httpx
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)
from mcp.shared._httpx_utils import create_mcp_http_client

logger = logging.getLogger("cygnus.config")

_DEFAULT_URL = "http://localhost:8080/mcp"

# Comfortably longer than a cold start. The cost of being too generous is a
# slow failure; the cost of being too strict is a confident, evidence-free
# report — which is far worse for a product whose whole claim is evidence.
_DEFAULT_TIMEOUT_SECONDS = 90.0

# Streamed responses need their own, much longer budget: the read timeout
# bounds the gap between chunks, not the total call. Matches the mcp SDK's
# MCP_DEFAULT_SSE_READ_TIMEOUT, which is the value being replaced for
# connect/general operations only — that one was never the problem.
_MCP_SSE_READ_TIMEOUT = 300.0


def sagittarius_url() -> str:
    return os.getenv("SAGITTARIUS_MCP_URL", _DEFAULT_URL)


def mcp_auth_headers() -> dict[str, str]:
    """The Authorization header to send on every MCP connection to Sagittarius.

    MCP_BEARER_TOKEN is the same env var name and value Sagittarius reads to
    enforce bearer auth on /mcp (Sagittarius#16 / B.3). With it unset, this
    returns {} and every caller's behaviour is exactly what it was before
    B.3 — this side must deploy and be confirmed working before Sagittarius
    turns enforcement on, or every MCP call starts failing with 401.
    """
    token = os.getenv("MCP_BEARER_TOKEN")
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def sagittarius_timeout() -> float:
    raw = os.getenv("SAGITTARIUS_TIMEOUT_SECONDS")
    if not raw:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "SAGITTARIUS_TIMEOUT_SECONDS=%r is not a number; using %.0fs",
            raw,
            _DEFAULT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TIMEOUT_SECONDS


def sagittarius_connection_params() -> StreamableHTTPConnectionParams:
    return StreamableHTTPConnectionParams(
        url=sagittarius_url(),
        timeout=sagittarius_timeout(),
        headers=mcp_auth_headers() or None,
    )


@asynccontextmanager
async def mcp_http_client(headers: dict[str, str] | None = None):
    """The httpx.AsyncClient to hand a raw `streamable_http_client(..., http_client=...)`
    call — as an async context manager, entered with `async with`.

    ADK's own StreamableHTTPConnectionParams takes `headers` directly (see
    sagittarius_connection_params above); the plain `mcp` SDK function used by
    Cygnus's own MCP clients — the evaluation worker and generation discovery
    — does not, so those build the client by hand instead. Centralised here
    so every raw MCP client goes through the same auth wiring; a caller
    inventing its own client-construction logic is exactly how a bearer token
    gets forgotten on one Sagittarius call site while every other one
    enforces it.

    Two things this has to get right that a bare httpx.AsyncClient(headers=...)
    doesn't:

    - Timeouts. The SDK's own default is MCP_DEFAULT_TIMEOUT = 30s for general
      operations (connect included) with a 300s SSE read. That is better than
      httpx's flat 5s, but **still below the ~50s a free-plan Sagittarius takes
      to wake**, which this docstring previously named as the hazard and then
      failed to clear.

      What that cost: the scheduled evaluation cron failed on 2026-09-22 with
      `{"evaluated":0,"reports_due":1,"price_unavailable":1,"degraded":true}`,
      after a 33-second run — 30s of connect timeout plus overhead, against a
      market that was open and priced the whole time (verified against Gamma).
      The same ceiling sits under SagittariusDiscovery, so a cold Sagittarius
      also turns the personalised feed into 503 sagittarius-unavailable.

      So the timeout is now sagittarius_timeout() — the same
      SAGITTARIUS_TIMEOUT_SECONDS (90s on Render) that render.yaml already
      sets *for exactly this reason*, and that previously reached only ADK's
      toolset path via sagittarius_connection_params(). Two of the three MCP
      client paths were ignoring the setting meant to cover them.
    - Lifecycle. streamable_http_client only closes a client it created
      itself ("Only manage client lifecycle if we created it" — a client
      passed in via http_client= is the caller's to close. A plain function
      returning an AsyncClient meant every call leaked a connection pool);
      this is why it's a context manager rather than a function, so the
      caller's `async with mcp_http_client(...) as http_client:` guarantees
      it's closed on the way out.

    Always yields a client, never None. It used to yield None when there were
    no headers to add, letting the SDK build its own default — but that default
    is the 30s one, so "no bearer token configured" silently also meant "cannot
    survive a cold start". The timeout matters whether or not auth does.
    """
    resolved = headers if headers is not None else mcp_auth_headers()
    timeout = httpx.Timeout(sagittarius_timeout(), read=_MCP_SSE_READ_TIMEOUT)
    async with create_mcp_http_client(
        headers=resolved or None, timeout=timeout
    ) as client:
        yield client


def warm_sagittarius(timeout: float = 60.0) -> bool:
    """Best-effort wake-up call at startup.

    Hits /health rather than /mcp: it is cheap, needs no MCP session, and
    waking the container is the entire point. Never raises — a failure here
    only means the first analysis pays the cold start it would have paid
    anyway.
    """
    url = sagittarius_url()
    health = url.rsplit("/mcp", 1)[0] + "/health"
    try:
        response = httpx.get(health, timeout=timeout)
        ok = response.status_code == 200
        logger.info(
            "warmed Sagittarius at %s (status %s)", health, response.status_code
        )
        return ok
    except Exception as exc:  # noqa: BLE001 — startup must not fail on this
        logger.warning("could not warm Sagittarius at %s: %s", health, exc)
        return False
