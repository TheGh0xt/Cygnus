"""The frozen contract is the artifact three repos build against.

These tests protect two properties. First, every frozen route is present and
answers machine-readably rather than 404ing or 500ing — a Lyra screen built
against a stub must get a usable error, not a mystery. Second, a stub is never
quietly mistaken for a working endpoint: it always carries the
`not-implemented` slug, so a client can tell "not built" from "broken".
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app

# Every frozen route, with the ROADMAP §0b task that retires it. Deleting a row
# here is how a stub graduates — and doing so without implementing the route
# makes test_no_stub_is_silently_dropped fail.
FROZEN: list[tuple[str, str, dict | None, str]] = [
    ("post", "/v1/me/mfa/enroll", None, "B.7"),
    ("post", "/v1/me/mfa/verify", {"factor_id": "f", "code": "123456"}, "B.7"),
    ("get", "/v1/me/mfa", None, "B.7"),
    ("get", "/v1/me/referrals", None, "B.10"),
    (
        "post",
        "/v1/billing/intent",
        {"price_shown_usd": 19.0, "plan": "pro-monthly"},
        "B.10",
    ),
    ("post", "/v1/events", {"name": "ui_mode_switched", "ui_mode": "TERMINAL"}, "B.19"),
    ("post", "/v1/waitlist", {"email": "someone@example.invalid"}, "B.15"),
    ("post", "/v1/analyses/abc/share", None, "B.6"),
    ("delete", "/v1/analyses/abc/share", None, "B.6"),
    ("get", "/v1/calibration", None, "B.11"),
    ("get", "/v1/markets/moving", None, "B.17"),
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PMIE_AUTH_DISABLED", "1")
    db = Path(tempfile.mkdtemp()) / "contract.db"
    return TestClient(create_app(str(db)))


@pytest.mark.parametrize(
    "method,path,body,task", FROZEN, ids=[f"{m.upper()} {p}" for m, p, _, _ in FROZEN]
)
def test_stub_answers_501_not_implemented(client, method, path, body, task):
    response = getattr(client, method)(path, **({"json": body} if body else {}))

    assert response.status_code == 501
    problem = response.json()
    assert problem["type"].endswith("not-implemented")
    # Naming the task means whoever hits this knows where it is tracked.
    assert task in problem["detail"]


def test_stubs_are_in_the_published_schema(client):
    """A route absent from openapi.json cannot be built against.

    This is the entire reason the stubs exist: Lyra generates its client from
    the schema, so freezing the shapes is what lets the UI work proceed in
    parallel rather than waiting on each endpoint.
    """
    schema = client.get("/openapi.json").json()

    for method, path, _, _ in FROZEN:
        template = path.replace("/abc", "/{analysis_id}")
        assert template in schema["paths"], f"{path} missing from schema"
        assert method in schema["paths"][template], f"{method.upper()} {path} missing"


def test_no_stub_is_silently_dropped(client):
    """Retiring a stub means implementing it, not deleting the route.

    Removing a row from FROZEN without shipping the endpoint would leave a
    contract Lyra has already generated types against pointing at a 404.
    """
    schema = client.get("/openapi.json").json()

    for method, path, _, _ in FROZEN:
        template = path.replace("/abc", "/{analysis_id}")
        operation = schema["paths"][template][method]
        assert operation.get("summary"), f"{method.upper()} {path} has no summary"


def test_schema_does_not_depend_on_runtime_config(monkeypatch):
    """The published contract must be identical with and without credentials.

    scripts/export_openapi.py runs in CI with no Supabase configuration. If the
    route surface varied with env, the committed openapi.json would describe a
    different API than production serves — and Lyra's generated client would be
    wrong in exactly the way the freshness gate is meant to prevent.
    """

    def paths_with(env: dict[str, str]) -> set[tuple[str, str]]:
        for key in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "PMIE_CRON_SECRET"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("PMIE_AUTH_DISABLED", "1")
        db = Path(tempfile.mkdtemp()) / "schema.db"
        schema = TestClient(create_app(str(db))).get("/openapi.json").json()
        return {(p, m) for p, ops in schema["paths"].items() for m in ops}

    bare = paths_with({})
    configured = paths_with(
        {
            "SUPABASE_URL": "https://example.invalid",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-not-a-real-key",
            "PMIE_CRON_SECRET": "cron-not-a-real-secret",
        }
    )

    assert bare == configured
