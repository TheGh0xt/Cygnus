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
from datetime import UTC, datetime
from typing import Protocol

from ..memory.store import SqliteMemoryStore
from ..schemas.report import MarketAnalysisReport

CONFIDENCE_INCREMENT = 0.05
CONFIDENCE_DECREMENT = 0.10
REVERSAL_TOLERANCE = 0.02  # price band within which a move counts as held


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


def run_evaluation_cycle(
    store: SqliteMemoryStore,
    prices: PriceFetcher,
    now: datetime | None = None,
) -> int:
    """Evaluates every due report; returns the number evaluated.

    Reports whose current price cannot be fetched are left due for the next
    cycle rather than being guessed at.
    """
    now = now or datetime.now(tz=UTC)
    evaluated = 0

    for stored in store.get_reports_due_for_evaluation(now):
        current = prices.current_probability(stored.market_slug)
        if current is None:
            continue

        result = evaluate_report(stored.report, stored.price_at_report, current)
        store.record_evaluation(
            stored.id,
            new_confidence=result.new_confidence,
            outcome=result.outcome,
            evaluated_at=now,
        )
        evaluated += 1

    return evaluated


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
        from mcp.client.streamable_http import streamablehttp_client

        try:
            async with streamablehttp_client(self.mcp_url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "get_event_by_slug", {"slug": market_slug}
                    )
                    payload = json.loads(result.content[0].text)
                    markets = payload.get("markets") or []
                    if not markets:
                        return None
                    return markets[0].get("probability")
        except Exception:
            return None


def main() -> None:
    import os

    parser = argparse.ArgumentParser(description="PMIE T+48h evaluation worker")
    parser.add_argument("--db", required=True, help="path to the memory store SQLite db")
    args = parser.parse_args()

    store = SqliteMemoryStore(args.db)
    fetcher = SagittariusPriceFetcher(
        os.getenv("SAGITTARIUS_MCP_URL", "http://localhost:8080/mcp")
    )
    count = run_evaluation_cycle(store, fetcher)
    print(f"evaluated {count} report(s)")


if __name__ == "__main__":
    main()
