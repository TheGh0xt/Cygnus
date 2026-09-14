"""aal2 enforcement for enrolled users — ROADMAP B.7 (coordinator review on #30).

A user who has completed TOTP enrolment must present an aal2 (stepped-up)
token everywhere except the two endpoints that let them get there: verify and
status. This lives in the auth gate itself (access.py), not in mfa_routes.py,
so it protects every endpoint uniformly rather than depending on each new
route remembering its own check — the same reasoning B.6 already applies to
identity itself.

A throwaway probe route stands in for "any normal endpoint": the point of
this suite is the gate's behaviour, not any particular handler's.
"""

from __future__ import annotations

import os

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from src.api.access import get_current_user
from src.api.app import create_app
from src.api.auth import CurrentUser
from src.api.mfa import FactorLookup, MfaError


class FakeJwks:
    def __init__(self):
        self.tokens: dict[str, CurrentUser] = {}

    def verify(self, token: str) -> CurrentUser:
        return self.tokens[token]


class FakeFactorLookup(FactorLookup):
    def __init__(self, enrolled: bool = False, error: Exception | None = None):
        super().__init__(base_url="https://example.test", service_key="key")
        self.enrolled = enrolled
        self.error = error
        self.calls: list[str] = []
        self.invalidated: list[str] = []

    def has_verified_totp_factor(self, user_id: str) -> bool:
        self.calls.append(user_id)
        if self.error:
            raise self.error
        return self.enrolled

    def invalidate(self, user_id: str) -> None:
        self.invalidated.append(user_id)


def _client(tmp_path, enrolled=False, error=None):
    os.environ.pop("PMIE_AUTH_DISABLED", None)
    app = create_app(db_path=str(tmp_path / "aal.db"))
    fake_jwks = FakeJwks()
    app.state.jwks = fake_jwks
    lookup = FakeFactorLookup(enrolled=enrolled, error=error)
    app.state.mfa_factor_lookup = lookup

    probe = APIRouter(dependencies=[Depends(get_current_user)])

    @probe.get("/v1/__aal_probe__")
    async def _probe():
        return {"ok": True}

    app.include_router(probe)

    return TestClient(app), fake_jwks, lookup


def _auth(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


class TestAal2Enforcement:
    def test_aal1_with_verified_factor_is_rejected(self, tmp_path):
        client, jwks, _ = _client(tmp_path, enrolled=True)
        jwks.tokens["t1"] = CurrentUser(id="u1", email=None, aal="aal1")

        response = client.get("/v1/__aal_probe__", headers=_auth("t1"))

        assert response.status_code == 401
        assert response.json()["type"].endswith("mfa-required")

    def test_aal2_is_allowed_through(self, tmp_path):
        client, jwks, _ = _client(tmp_path, enrolled=True)
        jwks.tokens["t1"] = CurrentUser(id="u1", email=None, aal="aal2")

        response = client.get("/v1/__aal_probe__", headers=_auth("t1"))

        assert response.status_code == 200

    def test_aal1_with_no_factor_is_allowed_through(self, tmp_path):
        # A user who has never enrolled has nothing to step up to.
        client, jwks, _ = _client(tmp_path, enrolled=False)
        jwks.tokens["t1"] = CurrentUser(id="u1", email=None, aal="aal1")

        response = client.get("/v1/__aal_probe__", headers=_auth("t1"))

        assert response.status_code == 200

    def test_aal1_can_still_reach_verify(self, tmp_path):
        client, jwks, _ = _client(tmp_path, enrolled=True)
        jwks.tokens["t1"] = CurrentUser(id="u1", email=None, aal="aal1")

        response = client.post(
            "/v1/me/mfa/verify",
            headers=_auth("t1"),
            json={"factor_id": "f", "code": "123456"},
        )

        # Not 401 — the request reaches the handler (which then does its own
        # thing; PMIE_AUTH_DISABLED is off here and app.state.mfa is a real
        # SupabaseMfa with no config, so this 503s past the gate, which is
        # what proves the *gate* let it through).
        assert response.status_code != 401

    def test_aal1_can_still_reach_status(self, tmp_path):
        client, jwks, _ = _client(tmp_path, enrolled=True)
        jwks.tokens["t1"] = CurrentUser(id="u1", email=None, aal="aal1")

        response = client.get("/v1/me/mfa", headers=_auth("t1"))

        assert response.status_code != 401

    def test_aal1_with_verified_factor_cannot_enroll(self, tmp_path):
        """Enroll is deliberately NOT exempt: an aal1 caller who already has

        a verified factor must not be able to add a second one — that would
        let a stolen password-only token add an attacker's own authenticator.
        """
        client, jwks, _ = _client(tmp_path, enrolled=True)
        jwks.tokens["t1"] = CurrentUser(id="u1", email=None, aal="aal1")

        response = client.post("/v1/me/mfa/enroll", headers=_auth("t1"))

        assert response.status_code == 401
        assert response.json()["type"].endswith("mfa-required")

    def test_lookup_failure_is_503_not_open(self, tmp_path):
        client, jwks, _ = _client(tmp_path, error=MfaError("admin API unreachable"))
        jwks.tokens["t1"] = CurrentUser(id="u1", email=None, aal="aal1")

        response = client.get("/v1/__aal_probe__", headers=_auth("t1"))

        assert response.status_code == 503

    def test_no_factor_lookup_configured_skips_enforcement(self, tmp_path):
        """MFA lookup not wired at all (e.g. Supabase unconfigured) must not

        block every authenticated request — that would be a much bigger
        outage than the feature it's protecting. Mirrors how accounts.py and
        growth.py treat "not configured" elsewhere in this app.
        """
        os.environ.pop("PMIE_AUTH_DISABLED", None)
        app = create_app(db_path=str(tmp_path / "aal2.db"))
        fake_jwks = FakeJwks()
        app.state.jwks = fake_jwks
        # A real, unconfigured lookup (no base_url/service_key) — configured
        # is False, so has_verified_totp_factor is never even called.
        app.state.mfa_factor_lookup = FactorLookup(base_url="", service_key="")

        probe = APIRouter(dependencies=[Depends(get_current_user)])

        @probe.get("/v1/__aal_probe_unconfigured__")
        async def _probe():
            return {"ok": True}

        app.include_router(probe)
        client = TestClient(app)
        fake_jwks.tokens["t1"] = CurrentUser(id="u1", email=None, aal="aal1")

        response = client.get("/v1/__aal_probe_unconfigured__", headers=_auth("t1"))

        assert response.status_code == 200


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("PMIE_AUTH_DISABLED", raising=False)
