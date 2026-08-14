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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from ..memory import build_memory_store
from ..schemas.report import MarketAnalysisReport

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


def run_evaluation_cycle(
    store,
    prices: PriceFetcher,
    now: datetime | None = None,
) -> int:
    """Score every checkpoint that has come due; returns how many were scored.

    Reports whose current price cannot be fetched are left for the next cycle
    rather than being guessed at, and a report with no observation price is
    skipped permanently — there is no baseline to score it against.
    """
    now = now or datetime.now(tz=UTC)
    scored = 0

    for stored in store.get_reports_awaiting_any_horizon(now):
        if stored.price_at_report is None:
            # Persisted without a price because the fetch failed at report
            # time. That baseline cannot be reconstructed, so the report is
            # permanently unscoreable.
            continue

        pending = due_horizons(
            stored.created_at, now, store.get_recorded_horizons(stored.id)
        )
        if not pending:
            continue

        current = prices.current_probability(stored.market_slug)
        if current is None:
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

    return scored


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
            # evaluation run. Structured logging arrives with Phase 1.9.
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
    count = run_evaluation_cycle(store, fetcher)
    print(f"evaluated {count} report(s)")


if __name__ == "__main__":
    main()
