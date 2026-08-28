"""Layer 5 evaluation worker — T+48h confidence backtesting.

Implements the deterministic verification matrix from docs/docs_AGENT_SPEC.md
section 4: a stored explanation implicitly claims the observed move had a real
cause. If the market price has held or extended 48 hours later, the
explanation is CONFIRMED and its confidence is incremented; if the move
reversed (e.g. the whale was rebalancing or running short-lived arbitrage),
it is REVERSED and confidence is decremented. Updated confidence flows back
into the memory store, closing the self-correction loop.

Run as a cron job:  python -m src.evaluation.worker --db pmie_memory.db
"""

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from ..memory import build_memory_store
from ..schemas.report import MarketAnalysisReport

logger = logging.getLogger("cygnus.evaluation")

CONFIDENCE_INCREMENT = 0.05
CONFIDENCE_DECREMENT = 0.10
REVERSAL_TOLERANCE = 0.02  # price band within which a move counts as held

# Escalating checkpoints, in hours.
#
# A single check at T+48h answers "was it right" and nothing else. Four
# checkpoints answer how durable the explanation was: "held at 12h, held at
# 18h, reversed by 48h" and "wrong from the start" are different results that
# one checkpoint records identically. It also accumulates data roughly four
# times faster, and the accuracy record is gated on wall-clock time.
EVALUATION_HORIZONS = (12, 18, 24, 48)

# The horizon the published accuracy record is computed from, and the only one
# that adjusts a report's confidence.
#
# Confidence must move once. The +0.05 / -0.10 matrix was designed for a
# single application; running it at every horizon would swing scores four
# times as far and let a report that wobbles whipsaw its own confidence.
# Earlier checkpoints are observations, deliberately not score changes.
CANONICAL_HORIZON = 48

# Prediction markets are noisy intraday, so a 12h checkpoint will disagree
# with the 48h one fairly often. That disagreement is data about durability,
# not a defect — which is exactly why 48h alone is authoritative.


def _extract_probability_from_tool_result(result: object) -> float | None:
    """Return the first market probability from a successful text MCP result."""
    from mcp.types import CallToolResult, TextContent

    if not isinstance(result, CallToolResult) or result.isError:
        return None

    text_block = next(
        (block for block in result.content if isinstance(block, TextContent)), None
    )
    if text_block is None:
        return None

    try:
        payload = json.loads(text_block.text)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None

    markets = payload.get("markets")
    if not isinstance(markets, list) or not markets:
        return None

    first_market = markets[0]
    if not isinstance(first_market, dict):
        return None

    probability = first_market.get("probability")
    return (
        float(probability)
        if isinstance(probability, int | float) and not isinstance(probability, bool)
        else None
    )


@dataclass
class EvalResult:
    outcome: str  # "CONFIRMED" | "REVERSED"
    new_confidence: float  # clamped to [0.0, 1.0]


def evaluate_report(
    report: MarketAnalysisReport,
    price_at_report: float,
    current_price: float,
) -> EvalResult:
    delta = current_price - price_at_report

    # "Same direction" is measured away from 0.5: a market above 0.5 that keeps
    # climbing (or one below 0.5 that keeps falling) is an extended move.
    if price_at_report >= 0.5:
        extended = delta > 0
    else:
        extended = delta < 0

    held = abs(delta) <= REVERSAL_TOLERANCE

    if held or extended:
        outcome = "CONFIRMED"
        new_confidence = min(1.0, report.confidence_score + CONFIDENCE_INCREMENT)
    else:
        outcome = "REVERSED"
        new_confidence = max(0.0, report.confidence_score - CONFIDENCE_DECREMENT)

    return EvalResult(outcome=outcome, new_confidence=new_confidence)


class PriceFetcher(Protocol):
    def current_probability(self, market_slug: str) -> float | None: ...


def due_horizons(
    created_at: datetime,
    now: datetime,
    already_recorded: set[int],
    horizons: tuple[int, ...] = EVALUATION_HORIZONS,
) -> list[int]:
    """Horizons that have elapsed for this report and are not yet recorded.

    Pure, so the scheduling rule is testable without a store or a clock.
    """
    return [
        horizon
        for horizon in horizons
        if horizon not in already_recorded
        and now - created_at >= timedelta(hours=horizon)
    ]


@dataclass(frozen=True)
class CycleReport:
    """What one evaluation cycle did, and whether it was able to do it.

    A bare count cannot answer the only question that matters when the number
    is zero: was there nothing to score, or could nothing be scored? Those are
    a healthy cycle and a broken one, and they must not report identically.
    """

    scored: int
    reports_due: int
    price_unavailable: int

    @property
    def is_degraded(self) -> bool:
        """True when a report came due and its price could not be fetched.

        Deliberately not "scored == 0": a cycle with nothing due scores zero
        and is perfectly healthy, while a cycle that scored some markets and
        failed others is already telling us the price source is flaky.
        """
        return self.price_unavailable > 0


def run_evaluation_cycle(
    store,
    prices: PriceFetcher,
    now: datetime | None = None,
) -> CycleReport:
    """Score every checkpoint that has come due.

    Reports whose current price cannot be fetched are left for the next cycle
    rather than being guessed at, and a report with no observation price is
    skipped permanently — there is no baseline to score it against.

    Both skips are counted rather than silent. An unreachable price source
    fails exactly like an idle weekend otherwise, which is how this cycle ran
    green for days while writing nothing.
    """
    now = now or datetime.now(tz=UTC)
    scored = 0
    reports_due = 0
    price_unavailable = 0

    for stored in store.get_reports_awaiting_any_horizon(now):
        if stored.price_at_report is None:
            # Persisted without a price because the fetch failed at report
            # time. That baseline cannot be reconstructed, so the report is
            # permanently unscoreable. Not a fetch failure: counting it as one
            # would raise an alarm that no amount of fixing could ever clear.
            logger.warning(
                "report %s has no price_at_report; permanently unscoreable",
                stored.id,
            )
            continue

        pending = due_horizons(
            stored.created_at, now, store.get_recorded_horizons(stored.id)
        )
        if not pending:
            continue

        reports_due += 1

        current = prices.current_probability(stored.market_slug)
        if current is None:
            # The report stays due and is retried next cycle. Left unrecorded
            # this is invisible, and an outage that never resolves is invisible
            # forever.
            price_unavailable += 1
            logger.warning(
                "no current price for %r (report %s); %d horizon(s) stay due",
                stored.market_slug,
                stored.id,
                len(pending),
            )
            continue

        for horizon in pending:
            result = evaluate_report(stored.report, stored.price_at_report, current)
            is_canonical = horizon == CANONICAL_HORIZON
            store.record_checkpoint(
                report_id=stored.id,
                horizon_hours=horizon,
                observed_price=current,
                outcome=result.outcome,
                evaluated_at=now,
                # Only the canonical horizon moves the score.
                new_confidence=result.new_confidence if is_canonical else None,
            )
            scored += 1

    return CycleReport(
        scored=scored,
        reports_due=reports_due,
        price_unavailable=price_unavailable,
    )


class SagittariusPriceFetcher:
    """Fetches the current probability of a market via Sagittarius MCP.

    Network shim, exercised by e2e validation rather than unit tests. Uses the
    first market's probability from get_event_by_slug's
    EventIntelligenceContext.
    """

    def __init__(self, mcp_url: str):
        self.mcp_url = mcp_url

    def current_probability(self, market_slug: str) -> float | None:
        import asyncio

        return asyncio.run(self._fetch(market_slug))

    async def _fetch(self, market_slug: str) -> float | None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        try:
            async with (
                streamable_http_client(self.mcp_url) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await session.call_tool(
                    "get_event_by_slug", {"slug": market_slug}
                )
                return _extract_probability_from_tool_result(result)
        except Exception:  # noqa: BLE001 — deliberate degraded path
            # Any failure to reach Sagittarius or parse its reply means "no
            # price observed": the caller leaves the report due and retries on
            # the next cycle rather than scoring it against missing data.
            # Narrowing this would let a new transport error abort a whole
            # evaluation run.
            #
            # It is logged with the URL it tried, because the failure this
            # path most often hides is a misconfigured SAGITTARIUS_MCP_URL --
            # unset, it silently defaults to localhost, which resolves to
            # nothing inside a container and refuses instantly. Swallowed
            # without a trace, that is indistinguishable from a market that
            # has simply gone away, and it stays that way for as long as
            # nobody thinks to look. The URL carries no credentials.
            logger.warning(
                "price fetch failed for %r via %s",
                market_slug,
                self.mcp_url,
                exc_info=True,
            )
            return None


def main() -> None:
    import os

    parser = argparse.ArgumentParser(description="PMIE T+48h evaluation worker")
    parser.add_argument(
        "--db", required=True, help="path to the memory store SQLite db"
    )
    args = parser.parse_args()

    store = build_memory_store(args.db)
    fetcher = SagittariusPriceFetcher(
        os.getenv("SAGITTARIUS_MCP_URL", "http://localhost:8080/mcp")
    )
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    result = run_evaluation_cycle(store, fetcher)
    print(
        f"evaluated {result.scored} checkpoint(s) "
        f"across {result.reports_due} due report(s)"
    )
    if result.is_degraded:
        # Non-zero exit so a cron wrapper treats an unreachable price source
        # as the failure it is rather than a quiet run.
        print(
            f"WARNING: {result.price_unavailable} report(s) could not be priced "
            f"via {fetcher.mcp_url}"
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
