"""The scheduled evaluation trigger.

Layer 5 only produces an accuracy record if it runs on a cadence. On a free
container plan the service sleeps when idle, so an in-process scheduler never
fires and platform cron is a paid feature. A scheduled GitHub Actions
workflow calls this endpoint instead.

It spends Sagittarius calls and rewrites the scores the product's claims rest
on, so it must never be reachable without the secret.
"""

import os

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app

SECRET = "a-long-shared-secret-value"


@pytest.fixture
def client(tmp_path, monkeypatch):
    os.environ["PMIE_AUTH_DISABLED"] = "1"
    monkeypatch.setenv("PMIE_CRON_SECRET", SECRET)
    app = create_app(db_path=str(tmp_path / "eval.db"))
    return TestClient(app)


class TestAuthorisation:
    def test_rejects_a_caller_with_no_secret(self, client):
        assert client.post("/v1/internal/evaluations/run").status_code == 401

    def test_rejects_a_wrong_secret(self, client):
        response = client.post(
            "/v1/internal/evaluations/run", headers={"x-cron-secret": "guess"}
        )
        assert response.status_code == 401

    def test_rejects_an_empty_secret_header(self, client):
        response = client.post(
            "/v1/internal/evaluations/run", headers={"x-cron-secret": "   "}
        )
        assert response.status_code == 401

    def test_accepts_the_configured_secret(self, client):
        response = client.post(
            "/v1/internal/evaluations/run", headers={"x-cron-secret": SECRET}
        )
        assert response.status_code == 200
        assert "evaluated" in response.json()

    def test_closed_when_no_secret_is_configured(self, tmp_path, monkeypatch):
        # An unconfigured deployment must not expose a trigger that spends
        # money and mutates scores, even to a caller sending nothing.
        os.environ["PMIE_AUTH_DISABLED"] = "1"
        monkeypatch.delenv("PMIE_CRON_SECRET", raising=False)
        unconfigured = TestClient(create_app(db_path=str(tmp_path / "closed.db")))

        response = unconfigured.post("/v1/internal/evaluations/run")
        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/problem+json")


class TestCycle:
    def test_reports_how_many_were_scored(self, client):
        # Nothing is due in a fresh store, and zero is a valid answer rather
        # than an error — most runs will score nothing.
        body = client.post(
            "/v1/internal/evaluations/run", headers={"x-cron-secret": SECRET}
        ).json()
        assert body["evaluated"] == 0

    def test_a_failing_cycle_surfaces_as_an_error(self, client, monkeypatch):
        # Reports stay due and retry next run, so the cost of failure is
        # delay rather than lost data — but it must not report success.
        def boom(*args, **kwargs):
            raise RuntimeError("store unreachable")

        monkeypatch.setattr("src.api.evaluation_routes.run_evaluation_cycle", boom)

        response = client.post(
            "/v1/internal/evaluations/run", headers={"x-cron-secret": SECRET}
        )
        assert response.status_code == 500
        assert "remain due" in response.json()["detail"]


class TestExposure:
    def test_absent_from_the_public_contract(self, client):
        # An internal trigger has no business in the docs the UI is built from.
        spec = client.get("/openapi.json").json()
        assert not any(p.startswith("/v1/internal") for p in spec["paths"])
