"""Storing a completed report with the price observed at the time.

Lives here rather than in an ADK callback because of where the data actually
is. The analyst writes its result via `output_key`, which ADK puts on the
event's `state_delta`; `CallbackContext.state` is session state plus the
callback's own empty delta, so an after-agent callback only sees the report
once ADK has committed that event to the session — which, in a real run, has
not happened yet. The callback saw nothing and returned quietly, so every
completed analysis was discarded while the run reported success.

The pipeline accumulates deltas from the events themselves and has the report
in hand, so persistence belongs there.
"""

from __future__ import annotations

import concurrent.futures
import logging

from ..schemas.report import MarketAnalysisReport

logger = logging.getLogger("cygnus.api.persistence")

# The price fetch runs in a worker thread: SagittariusPriceFetcher calls
# asyncio.run(), which raises inside the API's running event loop.
_PRICE_TIMEOUT_SECONDS = 30


class ReportPersistence:
    def __init__(self, store, price_fetcher):
        self._store = store
        self._price_fetcher = price_fetcher

    def save(self, report: dict | MarketAnalysisReport, slug: str) -> None:
        validated = (
            report
            if isinstance(report, MarketAnalysisReport)
            else MarketAnalysisReport.model_validate(report)
        )
        price = self._fetch_price(slug)
        self._store.save_report(validated, slug or "", price)
        logger.info(
            "persisted report for %s (price %s)",
            slug or "<unknown slug>",
            "unavailable" if price is None else price,
        )

    def _fetch_price(self, slug: str) -> float | None:
        if not slug:
            return None
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    self._price_fetcher.current_probability, slug
                ).result(timeout=_PRICE_TIMEOUT_SECONDS)
        except Exception:
            # Degraded-but-successful: store the report anyway. A null price
            # costs one unscoreable row; dropping the report loses it forever.
            logger.exception("price fetch failed; persisting without price")
            return None
