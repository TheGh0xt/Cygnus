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

import httpx
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)

logger = logging.getLogger("cygnus.config")

_DEFAULT_URL = "http://localhost:8080/mcp"

# Comfortably longer than a cold start. The cost of being too generous is a
# slow failure; the cost of being too strict is a confident, evidence-free
# report — which is far worse for a product whose whole claim is evidence.
_DEFAULT_TIMEOUT_SECONDS = 90.0


def sagittarius_url() -> str:
    return os.getenv("SAGITTARIUS_MCP_URL", _DEFAULT_URL)


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
    )


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
