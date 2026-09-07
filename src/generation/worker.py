"""The generation cycle — ROADMAP 4.12.

Ask Sagittarius what is moving, decide what is worth analysing, and run the
analysis pipeline over it. One cycle, on a schedule, is what turns an idle
evaluation engine into an accuracy record.

The cycle returns a structured report, never a count. On 2026-08-20 a bare
`{"evaluated": 0}` from the evaluation cycle was read as a silent failure when
it was the correct answer, and the misdiagnosis survived in three documents
for five days. Every outcome here carries a reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .discovery import DiscoveryError
from .selector import (
    COOLDOWN_HOURS,
    Candidate,
    Skip,
    SkipReason,
    select_for_analysis,
)

logger = logging.getLogger("cygnus.generation.worker")

# How many markets to ask Sagittarius for. Wider than the per-cycle limit so
# that markets in cooldown can be passed over without the cycle running dry.
DISCOVERY_LIMIT = 20


@dataclass
class GenerationReport:
    generated: int = 0
    skipped: list[Skip] = field(default_factory=list)
    # Distinct from "everything was skipped": discovery returned nothing at
    # all, which points at Sagittarius or a quiet market, not at these rules.
    no_candidates: bool = False
    # Set when discovery itself failed. The cycle still returns normally —
    # a delay is cheaper than a failed run — but never silently.
    discovery_error: str | None = None

    def as_dict(self) -> dict:
        """The shape the cron endpoint returns and the workflow log shows."""
        reasons: dict[str, int] = {}
        for skip in self.skipped:
            reasons[skip.reason.value] = reasons.get(skip.reason.value, 0) + 1
        return {
            "generated": self.generated,
            "no_candidates": self.no_candidates,
            "discovery_error": self.discovery_error,
            "skipped": [
                {"market_slug": s.market_slug, "reason": s.reason.value}
                for s in self.skipped
            ],
            "skipped_by_reason": reasons,
        }


async def run_generation_cycle(
    store,
    discovery,
    pipeline,
    registry,
    now: datetime | None = None,
    limit: int = 2,
    daily_cap: int = 8,
    categories: list[str] | None = None,
) -> GenerationReport:
    """Discover, select, and analyse. Returns what happened and why."""
    now = now or datetime.now(tz=UTC)

    try:
        candidates = await discovery.moving_markets(
            categories=categories, limit=DISCOVERY_LIMIT
        )
    except DiscoveryError as exc:
        logger.error("discovery failed; no analyses generated this cycle: %s", exc)
        return GenerationReport(discovery_error=str(exc))

    recent = store.recent_analyses(since=_lookback_from(now))
    selection = select_for_analysis(
        candidates, recent, now=now, limit=limit, daily_cap=daily_cap
    )

    report = GenerationReport(
        skipped=list(selection.skipped),
        no_candidates=selection.no_candidates,
    )

    for candidate in selection.selected:
        if await _analyse(candidate, pipeline, registry):
            report.generated += 1
        else:
            report.skipped.append(
                Skip(candidate.market_slug, SkipReason.PIPELINE_FAILED)
            )

    logger.info(
        "generation cycle complete: %d generated, %d skipped %s",
        report.generated,
        len(report.skipped),
        report.as_dict()["skipped_by_reason"],
    )
    return report


async def _analyse(candidate: Candidate, pipeline, registry) -> bool:
    """Run one analysis, reporting success rather than raising.

    One market failing must not cost the rest of the cycle: the others are
    still worth writing, and a report not written today can never be written
    later with today's price.
    """
    query = _query_for(candidate)
    record = registry.create(query)

    try:
        await pipeline.run(
            record.analysis_id, query, candidate.market_slug, profile_id=None
        )
    except Exception:
        logger.exception(
            "generated analysis failed for %s; continuing", candidate.market_slug
        )
        return False
    return True


def _query_for(candidate: Candidate) -> str:
    """Phrase the request the way a user would.

    The orchestrator routes on intent, and only a causal question reaches the
    analysis pipeline — "summarise this market" would land on a retrieval
    agent and never produce a MarketAnalysisReport, so the corpus would fill
    with the wrong artefact.
    """
    direction = "rose" if candidate.change_24h >= 0 else "fell"
    points = abs(candidate.change_24h) * 100
    return (
        f"Why did {candidate.market_slug} move today? "
        f"It {direction} {points:.1f} points in the last 24 hours "
        f"to {candidate.probability:.2f}."
    )


def _lookback_from(now: datetime) -> datetime:
    """How far back the store must be read.

    Far enough to answer both questions from one read: the cooldown needs the
    last COOLDOWN_HOURS, and the daily cap needs everything since midnight.
    Whichever is earlier wins — reading too short a window would under-count
    the cap and quietly let the ceiling be exceeded.
    """
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cooldown_start = now - timedelta(hours=COOLDOWN_HOURS)
    return min(start_of_day, cooldown_start)
