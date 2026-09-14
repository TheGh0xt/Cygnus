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
    call, or None — as an async context manager, entered with `async with`.

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

    - Timeouts. streamable_http_client's own default client is built via
      create_mcp_http_client(), which sets a 30s connect and a 300s read
      timeout plus follow_redirects=True. httpx's own default is a flat 5s
      and no redirects — fine for nothing here, and fatal against a
      Sagittarius that takes ~50s to wake from a free-plan sleep. Built with
      the same create_mcp_http_client() the SDK uses for its own default, so
      the two stay in lockstep as the SDK's defaults evolve.
    - Lifecycle. streamable_http_client only closes a client it created
      itself ("Only manage client lifecycle if we created it" — a client
      passed in via http_client= is the caller's to close. A plain function
      returning an AsyncClient meant every call leaked a connection pool);
      this is why it's a context manager rather than a function, so the
      caller's `async with mcp_http_client(...) as http_client:` guarantees
      it's closed on the way out.

    Yields None (not an empty-headers client) when there's nothing to add:
    the mcp SDK then builds and manages its own default client, matching
    behaviour from before B.3 exactly.
    """
    resolved = headers if headers is not None else mcp_auth_headers()
    if not resolved:
        yield None
        return
    async with create_mcp_http_client(headers=resolved) as client:
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
