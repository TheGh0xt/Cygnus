"""Agent lifecycle callbacks.

The analyst's after-callback is what makes Layer 5 possible: it writes each
completed report to the memory store together with the price observed at
the time. That price cannot be reconstructed later — a report not written
now is permanently unscoreable.
"""

from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Callable

from ..schemas.report import MarketAnalysisReport

logger = logging.getLogger("cygnus.agents.callbacks")


def resolve_slug_from_state(state: dict) -> str | None:
    """The slug the analysis was requested for.

    The API writes `event_slug` into session state before the run starts,
    so this does not have to parse it back out of LLM-authored text.
    """
    slug = state.get("event_slug")
    return slug if isinstance(slug, str) and slug else None


def make_persist_report_callback(store, price_fetcher) -> Callable:
    # The parameter name is part of the contract: ADK invokes after-agent
    # callbacks as `callback(callback_context=...)` by keyword, not
    # positionally (see BaseAgent._handle_after_agent_callback). Renaming
    # this parameter breaks the callback at runtime while unit tests that
    # call it positionally keep passing.
    def persist_report(callback_context):
        state = callback_context.state
        raw = state.get("market_analysis_report")
        if not raw:
            logger.warning("analyst produced no report; nothing to persist")
            return None

        slug = resolve_slug_from_state(state)
        if slug is None:
            logger.warning("no event_slug in state; storing report without slug")
            slug = ""

        try:
            report = (
                raw
                if isinstance(raw, MarketAnalysisReport)
                else MarketAnalysisReport.model_validate(raw)
            )
        except Exception:
            logger.exception("analyst output failed schema validation")
            return None

        price = _fetch_price(price_fetcher, slug)

        store.save_report(report, slug, price)
        logger.info("persisted report for %s", slug or "<unknown slug>")
        return None

    return persist_report


# The fetch runs in a worker thread on purpose. SagittariusPriceFetcher calls
# asyncio.run(), which raises "cannot be called from a running event loop" when
# invoked from inside the async API — so a direct call here always fails and
# every report would be stored with a null price, silently unscoreable. A
# fresh thread has no running loop, so asyncio.run() works there.
_PRICE_TIMEOUT_SECONDS = 30


def _fetch_price(price_fetcher, slug: str) -> float | None:
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(price_fetcher.current_probability, slug).result(
                timeout=_PRICE_TIMEOUT_SECONDS
            )
    except Exception:
        # Degraded-but-successful: persist the report anyway. A null price
        # means the evaluation worker skips it, which is strictly better than
        # losing the report entirely.
        logger.exception("price fetch failed; persisting without price")
        return None
