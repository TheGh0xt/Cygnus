"""The personalised feed — ROADMAP B.17.

Reuses the same Sagittarius client and Candidate type the scheduled
generation cycle already uses (src/generation/discovery.py): both ask
get_moving_markets, and there is no reason for two decoders of the same
tool contract to drift.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..generation.selector import Candidate
from .models import MovingMarket

# Sagittarius only sources from Polymarket today (B.4/B.5's source
# abstraction and Kalshi provider are deferred past the beta gate).
_SOURCE = "POLYMARKET"


def _days_to_resolution(
    end_date: str | None, now: datetime | None = None
) -> int | None:
    if not end_date:
        return None
    try:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    # A market resolving later today reads as "0 days away", not "-1" — a
    # negative count would look like a data bug rather than "soon".
    return max((end - reference).days, 0)


def to_moving_market(candidate: Candidate, now: datetime | None = None) -> MovingMarket:
    return MovingMarket(
        slug=candidate.market_slug,
        question=candidate.title,
        source=_SOURCE,
        probability=candidate.probability,
        change_24h=candidate.change_24h,
        volume_24h=candidate.volume_24h,
        category=candidate.category,
        days_to_resolution=_days_to_resolution(candidate.end_date, now),
    )
