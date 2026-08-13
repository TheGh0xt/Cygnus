"""The gate itself: with auth ON, protected routes must reject callers.

Separate from test_auth.py, which covers token decoding. This covers the
wiring — that the dependency is actually attached to the endpoints, which is
the part that silently regresses when someone adds a route.
"""

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("PMIE_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    return TestClient(create_app(db_path=str(tmp_path / "gate.db")))


PROTECTED = [
    ("post", "/v1/analyses", {"query": "why is world-cup-winner moving?"}),
    ("get", "/v1/me", None),
    ("put", "/v1/me/interests", {"categories": ["politics", "crypto", "ai"]}),
    ("post", "/v1/analyses/abc/feedback", {"is_useful": True}),
]


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_protected_routes_reject_anonymous_callers(client, method, path, body):
    response = (
        getattr(client, method)(path, json=body)
        if body
        else getattr(client, method)(path)
    )
    assert response.status_code == 401, f"{method.upper()} {path} was not protected"
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_protected_routes_reject_garbage_tokens(client, method, path, body):
    headers = {"authorization": "Bearer not-a-real-token"}
    call = getattr(client, method)
    response = (
        call(path, json=body, headers=headers) if body else call(path, headers=headers)
    )
    # 401 for a bad token; 503 if the JWKS endpoint itself is unreachable,
    # which is our failure and must not be reported as bad credentials.
    assert response.status_code in (401, 503)


def test_health_and_ready_stay_public(client):
    # Load balancers and uptime checks cannot authenticate.
    assert client.get("/v1/health").status_code == 200
    assert client.get("/v1/ready").status_code == 200


def test_auth_disabled_flag_is_read_once_at_startup(tmp_path, monkeypatch):
    # Set after the app is built: a later environment change must not be able
    # to switch authentication off in a running process.
    monkeypatch.delenv("PMIE_AUTH_DISABLED", raising=False)
    app = create_app(db_path=str(tmp_path / "startup.db"))
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "1")
    assert app.state.auth_disabled is False
    assert TestClient(app).get("/v1/me").status_code == 401
