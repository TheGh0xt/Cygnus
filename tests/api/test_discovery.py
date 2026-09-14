from datetime import UTC, datetime

from src.api.discovery import to_moving_market
from src.generation.selector import Candidate


def test_maps_candidate_fields_onto_the_contract_shape():
    candidate = Candidate(
        market_slug="who-will-bernie-endorse",
        title="Who will Bernie endorse?",
        category="Politics",
        probability=0.275,
        change_24h=0.336,
        volume_24h=1000.0,
        end_date="2026-11-04T00:00:00Z",
    )

    market = to_moving_market(candidate, now=datetime(2026, 11, 1, tzinfo=UTC))

    assert market.slug == "who-will-bernie-endorse"
    assert market.question == "Who will Bernie endorse?"
    assert market.source == "POLYMARKET"
    assert market.probability == 0.275
    assert market.change_24h == 0.336
    assert market.volume_24h == 1000.0
    assert market.category == "Politics"
    assert market.days_to_resolution == 3


def test_missing_end_date_leaves_days_to_resolution_unset():
    candidate = Candidate(
        market_slug="no-date",
        title="fine",
        category="Crypto",
        probability=0.4,
        change_24h=0.1,
    )

    market = to_moving_market(candidate)

    assert market.days_to_resolution is None


def test_unparseable_end_date_leaves_days_to_resolution_unset():
    candidate = Candidate(
        market_slug="bad-date",
        title="fine",
        category="Crypto",
        probability=0.4,
        change_24h=0.1,
        end_date="not-a-date",
    )

    market = to_moving_market(candidate)

    assert market.days_to_resolution is None


def test_a_market_resolving_today_is_zero_days_not_negative():
    candidate = Candidate(
        market_slug="today",
        title="fine",
        category="Crypto",
        probability=0.4,
        change_24h=0.1,
        end_date="2026-11-01T00:00:00Z",
    )

    market = to_moving_market(candidate, now=datetime(2026, 11, 1, 12, tzinfo=UTC))

    assert market.days_to_resolution == 0
