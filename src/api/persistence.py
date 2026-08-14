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
#
# The budget has to outlast a Sagittarius cold start. At 30s it did not, so
# every report was stored with a null price — permanently unscoreable, since
# the observed price cannot be recovered afterwards.
def _price_timeout() -> float:
    from ..config import sagittarius_timeout

    return sagittarius_timeout()


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

        # A report with no market identifier can never be scored: the
        # evaluation worker has nothing to fetch a later price for. Storing it
        # would put a permanently unscoreable row into the data the accuracy
        # record is computed from.
        #
        # This should be unreachable — the API rejects requests with no
        # identifiable market before any work begins — but the cost of being
        # wrong is silent pollution of the one dataset that matters.
        if not validated.market_id and not slug:
            logger.warning(
                "not storing an unscoreable report: no market_id and no slug. "
                "The analysis ran without market data."
            )
            return

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
                ).result(timeout=_price_timeout())
        except Exception:
            # Degraded-but-successful: store the report anyway. A null price
            # costs one unscoreable row; dropping the report loses it forever.
            logger.exception("price fetch failed; persisting without price")
            return None
