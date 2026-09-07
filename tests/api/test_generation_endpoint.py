"""The scheduled generation trigger — ROADMAP 4.12.

Same shape as the evaluation trigger and for the same reason: a free
container plan sleeps when idle, so an in-process scheduler never fires and
platform cron is paid. A scheduled GitHub Actions workflow calls this.

This one spends real money — every generated analysis is four Gemini stages
plus search grounding — so the secret matters more here than anywhere else in
the API, and the response has to say what actually happened.
"""

import os
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.generation.selector import Candidate

SECRET = "a-long-shared-secret-value"


@pytest.fixture
def client(tmp_path, monkeypatch):
    os.environ["PMIE_AUTH_DISABLED"] = "1"
    monkeypatch.setenv("PMIE_CRON_SECRET", SECRET)
    app = create_app(db_path=str(tmp_path / "gen.db"))
    return TestClient(app)


def _cron_hours(workflow_text: str) -> set[int]:
    """Hours a workflow's schedule fires, from either cron form."""
    match = re.search(r'- cron: "0 (\S+) \* \* \*"', workflow_text)
    assert match, "workflow has no recognisable hourly cron"
    field = match.group(1)
    if field.startswith("*/"):
        return set(range(0, 24, int(field[2:])))
    return {int(h) for h in field.split(",")}


class TestAuthorisation:
    def test_rejects_a_caller_with_no_secret(self, client):
        assert client.post("/v1/internal/generation/run").status_code == 401

    def test_rejects_a_wrong_secret(self, client):
        response = client.post(
            "/v1/internal/generation/run", headers={"x-cron-secret": "guess"}
        )
        assert response.status_code == 401

    def test_closed_when_no_secret_is_configured(self, tmp_path, monkeypatch):
        # An unconfigured deployment must not expose a trigger that spends
        # money, even to a caller who sends nothing.
        os.environ["PMIE_AUTH_DISABLED"] = "1"
        monkeypatch.delenv("PMIE_CRON_SECRET", raising=False)
        app = create_app(db_path=str(tmp_path / "gen.db"))
        response = TestClient(app).post("/v1/internal/generation/run")
        assert response.status_code == 503


class TestResponseShape:
    def test_reports_why_nothing_was_generated(self, client, monkeypatch):
        """The core requirement. A bare zero is what cost five days."""

        async def no_markets(*a, **kw):
            return []

        monkeypatch.setattr(client.app.state.discovery, "moving_markets", no_markets)

        response = client.post(
            "/v1/internal/generation/run", headers={"x-cron-secret": SECRET}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["generated"] == 0
        # "Nothing was moving" must be distinguishable from "it broke".
        assert body["no_candidates"] is True
        assert body["discovery_error"] is None

    def test_reports_a_discovery_failure_rather_than_a_bare_zero(
        self, client, monkeypatch
    ):
        from src.generation.discovery import DiscoveryError

        async def boom(*a, **kw):
            raise DiscoveryError("sagittarius unreachable")

        monkeypatch.setattr(client.app.state.discovery, "moving_markets", boom)

        response = client.post(
            "/v1/internal/generation/run", headers={"x-cron-secret": SECRET}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["generated"] == 0
        assert "unreachable" in body["discovery_error"]

    def test_reports_skips_with_their_reasons(self, client, monkeypatch):
        async def one_market(*a, **kw):
            return [
                Candidate("m1", "Market one", "Politics", 0.4, 0.2),
            ]

        monkeypatch.setattr(client.app.state.discovery, "moving_markets", one_market)
        # A cap of zero is the cheapest way to force a typed skip without
        # running the model.
        monkeypatch.setenv("PMIE_GENERATION_DAILY_CAP", "0")

        response = client.post(
            "/v1/internal/generation/run", headers={"x-cron-secret": SECRET}
        )

        body = response.json()
        assert body["generated"] == 0
        assert body["skipped_by_reason"]["daily_cap_reached"] == 1


class TestSpendCeiling:
    def test_daily_cap_is_configurable(self, client, monkeypatch):
        captured = {}

        async def fake_cycle(**kwargs):
            captured.update(kwargs)
            from src.generation.worker import GenerationReport

            return GenerationReport()

        monkeypatch.setattr(
            "src.api.generation_routes.run_generation_cycle", fake_cycle
        )
        monkeypatch.setenv("PMIE_GENERATION_DAILY_CAP", "3")
        monkeypatch.setenv("PMIE_GENERATION_PER_CYCLE", "1")

        client.post("/v1/internal/generation/run", headers={"x-cron-secret": SECRET})

        assert captured["daily_cap"] == 3
        assert captured["limit"] == 1

    def test_defaults_match_the_agreed_budget(self, client, monkeypatch):
        # 24/day across eight 3-hourly cycles. Raised from 8/day so a
        # calibration curve is reachable in ~13 days rather than ~38; the
        # raise went into cadence rather than batch size, because reports
        # created together come due together and backfill from one price
        # observation.
        captured = {}

        async def fake_cycle(**kwargs):
            captured.update(kwargs)
            from src.generation.worker import GenerationReport

            return GenerationReport()

        monkeypatch.setattr(
            "src.api.generation_routes.run_generation_cycle", fake_cycle
        )
        monkeypatch.delenv("PMIE_GENERATION_DAILY_CAP", raising=False)
        monkeypatch.delenv("PMIE_GENERATION_PER_CYCLE", raising=False)

        client.post("/v1/internal/generation/run", headers={"x-cron-secret": SECRET})

        assert captured["daily_cap"] == 24
        assert captured["limit"] == 3

    def test_daily_cap_matches_the_cron_cadence(self):
        """The budget is per_cycle x cycles/day — keep the two in step.

        Raising the cron frequency without lowering the per-cycle limit (or
        vice versa) silently changes real Gemini spend, which is the exact
        drift the budget guard above exists to catch.
        """
        from src.api.generation_routes import _DEFAULT_DAILY_CAP, _DEFAULT_PER_CYCLE

        workflow = (
            pathlib.Path(__file__).resolve().parents[2]
            / ".github"
            / "workflows"
            / "generate.yml"
        ).read_text()

        generation_hours = _cron_hours(workflow)
        assert _DEFAULT_PER_CYCLE * len(generation_hours) == _DEFAULT_DAILY_CAP

    def test_generation_never_collides_with_evaluation(self):
        """The two crons are offset on purpose.

        Both wake a cold-starting Sagittarius, and a shared free instance
        cannot serve a generation fan-out and an evaluation sweep at once.
        The original 3/9/15/21 schedule encoded that offset; anything that
        re-derives the cadence has to preserve it.
        """
        workflows = (
            pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows"
        )
        generation = _cron_hours((workflows / "generate.yml").read_text())
        evaluation = _cron_hours((workflows / "evaluate.yml").read_text())

        assert generation & evaluation == set(), (
            f"generation and evaluation both fire at {sorted(generation & evaluation)}"
        )
