"""Full request lifecycle against a fake ADK runner.

No network, no Gemini, no Sagittarius — this must stay hermetic to pass the
--disable-socket CI job.
"""

import json

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.pipeline import AnalysisPipeline


class FakeActions:
    """Mirrors ADK's EventActions: output_key writes arrive as state_delta."""

    def __init__(self, state_delta=None):
        self.state_delta = state_delta or {}


class FakeEvent:
    def __init__(self, author, report=None, final=False):
        self.author = author
        self.actions = FakeActions({"market_analysis_report": report} if report else {})
        self._final = final or report is not None

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
        for author in (
            "analysis_event_retrieval",
            "analysis_signal_retrieval",
            "analysis_news_retrieval",
        ):
            yield FakeEvent(author)
        yield FakeEvent(
            "market_analyst_agent",
            # Must be a schema-valid MarketAnalysisReport: the response model
            # validates it, so an incomplete fake here would pass a test the
            # real pipeline could never satisfy.
            report={
                "market_id": "0xabc",
                "timestamp": "2026-08-12T00:00:00+00:00",
                "summary": "whale accumulation",
                "primary_causal_driver": "WHALE_ACTIVITY",
                "confidence_score": 0.75,
                "key_drivers": [
                    {
                        "type": "WHALE_ACTIVITY",
                        "impact": "HIGH",
                        "evidence_summary": "$92.3k net buy on the England market",
                    }
                ],
            },
        )


def _client(tmp_path):
    app = create_app(db_path=str(tmp_path / "e2e.db"))
    app.state.pipeline = AnalysisPipeline(app.state.registry, FakeRunner())
    return TestClient(app)


def test_create_stream_and_fetch_report(tmp_path):
    client = _client(tmp_path)

    created = client.post(
        "/v1/analyses", json={"query": "why is world-cup-winner moving?"}
    )
    assert created.status_code == 201
    analysis_id = created.json()["analysis_id"]
    assert created.json()["stream_url"].endswith(f"{analysis_id}/events")

    with client.stream("GET", f"/v1/analyses/{analysis_id}/events") as stream:
        body = "".join(chunk for chunk in stream.iter_text())

    assert "event: stage_started" in body
    assert "event: report" in body
    assert "event_retrieval" in body
    assert "analysis" in body

    result = client.get(f"/v1/analyses/{analysis_id}")
    assert result.status_code == 200
    payload = result.json()
    assert payload["status"] == "completed"
    assert payload["report"]["primary_causal_driver"] == "WHALE_ACTIVITY"


def test_request_id_header_is_returned(tmp_path):
    assert _client(tmp_path).get("/v1/health").headers.get("X-Request-ID")


def test_sse_payloads_are_valid_json(tmp_path):
    client = _client(tmp_path)
    analysis_id = client.post(
        "/v1/analyses", json={"query": "why is world-cup-winner moving?"}
    ).json()["analysis_id"]

    with client.stream("GET", f"/v1/analyses/{analysis_id}/events") as stream:
        for line in stream.iter_lines():
            if line.startswith("data: "):
                json.loads(line[len("data: ") :])
