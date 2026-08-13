from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.models import extract_slug


def test_extract_slug_from_polymarket_url():
    assert (
        extract_slug("https://polymarket.com/event/world-cup-winner", None)
        == "world-cup-winner"
    )


def test_extract_slug_prefers_explicit_field():
    assert extract_slug("anything", "explicit-slug") == "explicit-slug"


def test_extract_slug_falls_back_to_bare_token():
    assert extract_slug("why is world-cup-winner moving?", None) == "world-cup-winner"


def test_extract_slug_returns_empty_when_absent():
    assert extract_slug("why is it moving?", None) == ""


def test_health_and_ready(tmp_path):
    client = TestClient(create_app(db_path=str(tmp_path / "t.db")))
    assert client.get("/v1/health").json()["status"] == "ok"
    assert client.get("/v1/ready").status_code == 200


def test_unknown_analysis_returns_problem_json(tmp_path):
    client = TestClient(create_app(db_path=str(tmp_path / "t.db")))
    response = client.get("/v1/analyses/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"].endswith("analysis-not-found")


def test_create_analysis_rejects_short_query(tmp_path):
    client = TestClient(create_app(db_path=str(tmp_path / "t.db")))
    assert client.post("/v1/analyses", json={"query": "x"}).status_code == 422
