"""Deciding which discovered markets become analyses.

Pure — no store, no clock, no network — so the rules can be tested directly,
for the same reason `due_horizons` is pure in the evaluation worker.

Every rejection carries a typed reason. A cycle that reports only a count
cannot distinguish "nothing qualified" from "the upstream is broken", and on
2026-08-20 that ambiguity cost five days: `{"evaluated": 0}` was read as a
silent failure when it was in fact the correct answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from ..memory.store import RecentAnalysis

# How long a market waits before it may be analysed again.
#
# Short on purpose. An earlier draft suppressed re-analysis for seven days,
# which quietly fought the rest of Phase 4: 4.4 wants per-market historical
# accuracy, 4.3 wants a prior report to match against, and UI_PRD §6.5 ships
# the prompt starter "Compare today's move to the last time this market
# spiked." All three need a market to be analysed more than once.
#
# So this is not deduplication. It exists only to stop one violently volatile
# market taking every slot in a cycle.
COOLDOWN_HOURS = 12


class SkipReason(str, Enum):
    COOLDOWN = "cooldown"
    DAILY_CAP_REACHED = "daily_cap_reached"
    # Recorded by the cycle rather than the selector: the market was chosen,
    # and the analysis itself failed.
    PIPELINE_FAILED = "pipeline_failed"


@dataclass(frozen=True)
class Candidate:
    """One market Sagittarius reported as moving."""

    market_slug: str
    title: str
    category: str
    probability: float
    change_24h: float


@dataclass(frozen=True)
class Skip:
    market_slug: str
    reason: SkipReason


@dataclass
class Selection:
    selected: list[Candidate] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)
    # Distinct from "everything was skipped": nothing was offered at all,
    # which points at discovery rather than at these rules.
    no_candidates: bool = False


def select_for_analysis(
    candidates: list[Candidate],
    recent: list[RecentAnalysis],
    now: datetime,
    limit: int,
    daily_cap: int,
) -> Selection:
    """Choose up to `limit` candidates, respecting the cooldown and the cap.

    Order is preserved exactly as discovery supplied it. Sagittarius has
    already round-robined across categories and ranked by movement; re-sorting
    here would undo that and let one category dominate the corpus, which is
    what makes the per-category accuracy record unsliceable.
    """
    if not candidates:
        return Selection(no_candidates=True)

    cooling = _slugs_in_cooldown(recent, now)
    remaining = _remaining_today(recent, now, daily_cap)

    result = Selection()

    for candidate in candidates:
        if len(result.selected) >= limit:
            # Not rejected — simply not reached this cycle. Recording these as
            # skips would bury the real reasons under routine noise.
            break

        if candidate.market_slug in cooling:
            result.skipped.append(Skip(candidate.market_slug, SkipReason.COOLDOWN))
            continue

        if remaining <= 0:
            # Record it once and stop. Once the ceiling is reached no later
            # candidate can be selected either, so continuing would append a
            # skip per remaining candidate — twenty rows all saying the same
            # thing, burying the reasons that differ.
            result.skipped.append(
                Skip(candidate.market_slug, SkipReason.DAILY_CAP_REACHED)
            )
            break

        result.selected.append(candidate)
        remaining -= 1

    return result


def _slugs_in_cooldown(recent: list[RecentAnalysis], now: datetime) -> set[str]:
    cutoff = now - timedelta(hours=COOLDOWN_HOURS)
    return {r.market_slug for r in recent if r.created_at > cutoff}


def _remaining_today(
    recent: list[RecentAnalysis], now: datetime, daily_cap: int
) -> int:
    """How many analyses the ceiling still allows today.

    The ceiling is a safety valve against a double-fired cron, a retried
    workflow or a stray manual trigger — the cost of getting it wrong is a
    bill, so a cap of zero means zero rather than "unlimited".
    """
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    used = sum(1 for r in recent if r.created_at >= start_of_day)
    return daily_cap - used
