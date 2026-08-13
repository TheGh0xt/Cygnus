"""The limiter as wired into the endpoint.

test_ratelimit.py covers the algorithm. This covers that it is actually
attached to the expensive route and returns the right shape — the part that
regresses silently when someone edits routes.py.
"""

import os

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.pipeline import AnalysisPipeline
from src.api.ratelimit import RateLimiter


class FakeActions:
    def __init__(self, state_delta=None):
        self.state_delta = state_delta or {}


class FakeEvent:
    def __init__(self, author, report=None):
        self.author = author
        self.actions = FakeActions({"market_analysis_report": report} if report else {})
        self._final = report is not None

    def is_final_response(self):
        return self._final


class FakeSessionService:
    @staticmethod
    async def create_session(**kwargs):
        class S:
            id = kwargs.get("session_id", "s1")
            state = {}

        return S()


class FakeRunner:
    session_service = FakeSessionService()

    async def run_async(self, **kwargs):
        yield FakeEvent(
            "market_analyst_agent",
            report={
                "market_id": "0xabc",
                "timestamp": "2026-08-13T00:00:00+00:00",
                "summary": "s",
                "primary_causal_driver": "WHALE_ACTIVITY",
                "confidence_score": 0.75,
                "key_drivers": [],
            },
        )


@pytest.fixture
def client(tmp_path):
    os.environ["PMIE_AUTH_DISABLED"] = "1"
    app = create_app(db_path=str(tmp_path / "rl.db"))
    app.state.pipeline = AnalysisPipeline(app.state.registry, FakeRunner())
    # Two per window, so the third call is the interesting one.
    app.state.limiter = RateLimiter(per_user=2, per_user_window=3600)
    return TestClient(app)


def _start(client):
    return client.post(
        "/v1/analyses", json={"query": "why is world-cup-winner moving?"}
    )


def test_third_call_is_rejected(client):
    assert _start(client).status_code == 201
    assert _start(client).status_code == 201
    assert _start(client).status_code == 429


def test_rejection_is_problem_json_with_a_stable_type(client):
    _start(client)
    _start(client)
    response = _start(client)

    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"].endswith("rate-limited")
    assert body["status"] == 429


def test_rejection_tells_the_caller_when_to_retry(client):
    _start(client)
    _start(client)
    detail = _start(client).json()["detail"]
    assert "seconds" in detail


def test_limit_does_not_block_reads(client):
    # Only the expensive endpoint is limited; checking your own account or
    # reading a finished report must keep working.
    _start(client)
    _start(client)
    assert _start(client).status_code == 429
    assert client.get("/v1/health").status_code == 200
    assert client.get("/v1/analyses/unknown-id").status_code == 404
