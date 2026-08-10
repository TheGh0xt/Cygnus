"""Agent lifecycle callbacks.

The analyst's after-callback is what makes Layer 5 possible: it writes each
completed report to the memory store together with the price observed at
the time. That price cannot be reconstructed later — a report not written
now is permanently unscoreable.
"""

from __future__ import annotations

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
    def persist_report(ctx):
        raw = ctx.state.get("market_analysis_report")
        if not raw:
            logger.warning("analyst produced no report; nothing to persist")
            return None

        slug = resolve_slug_from_state(ctx.state)
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

        try:
            price = price_fetcher.current_probability(slug)
        except Exception:
            # Degraded-but-successful: persist the report anyway. A null
            # price means the evaluation worker skips it, which is strictly
            # better than losing the report entirely.
            logger.exception("price fetch failed; persisting without price")
            price = None

        store.save_report(report, slug, price)
        logger.info("persisted report for %s", slug or "<unknown slug>")
        return None

    return persist_report
