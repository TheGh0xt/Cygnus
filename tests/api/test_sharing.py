"""Owner-only reads, and reads via a share token — ROADMAP B.6.

Uses the same fake-runner harness as test_contract_e2e.py but with real
per-caller identity (not PMIE_AUTH_DISABLED's single fixed user), because the
whole point here is telling the owner apart from everyone else. A fake JWKS
verifier stands in for Supabase: it treats the bearer token itself as the
user id, so tests can mint "tokens" for two distinct users without any real
cryptography.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from src.api.accounts import Profile, ReferralCounts
from src.api.app import create_app
from src.api.auth import CurrentUser
from src.api.pipeline import AnalysisPipeline
from src.api.sharing import ShareTokens

from .test_contract_e2e import FakeRunner

OWNER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


class FakeJwks:
    """A bearer token here is literally the caller's user id."""

    def verify(self, token: str) -> CurrentUser:
        return CurrentUser(id=token, email=f"{token}@example.test")


class FakeShareTokens(ShareTokens):
    """ShareTokens with the HTTP layer replaced by an in-memory dict."""

    def __init__(self):
        super().__init__(base_url="https://example.test", service_key="key")
        self._tokens: dict[str, dict] = {}
        self._next = 0

    @property
    def configured(self) -> bool:  # noqa: D102 - always usable in tests
        return True

    def create(self, report_id, created_by, ttl_days=30):
        from datetime import UTC, datetime, timedelta

        for t in self._tokens.values():
            if t["report_id"] == report_id:
                t["revoked"] = True
        self._next += 1
        token = f"share-token-{self._next}"
        expires_at = (
            datetime.now(tz=UTC) + timedelta(days=ttl_days) if ttl_days else None
        )
        self._tokens[token] = {
            "report_id": report_id,
            "created_by": created_by,
            "revoked": False,
            "expires_at": expires_at,
        }
        from src.api.sharing import ShareToken

        return ShareToken(token=token, report_id=report_id, expires_at=expires_at)

    def resolve(self, token):
        row = self._tokens.get(token)
        if row is None or row["revoked"]:
            return None
        return row["report_id"]

    def revoke(self, report_id, revoked_by):
        for t in self._tokens.values():
            if t["report_id"] == report_id:
                t["revoked"] = True


class FakeAccounts:
    """Every caller is a real, invited user.

    test_contract_e2e reaches the same endpoints via PMIE_AUTH_DISABLED, which
    short-circuits the invite gate along with authentication. This suite cannot
    use that flag — it needs callers to be distinguishable, which is the whole
    subject here — so the invite gate runs for real and needs a backing store.
    Without this the analysis POST fails 503 "Accounts are not configured"
    before any sharing logic is reached.
    """

    configured = True

    def get_profile(self, user_id: str) -> Profile:
        return Profile(
            id=user_id,
            display_name="test",
            is_invited=True,
            is_grandfathered=False,
            onboarding_completed_at=None,
        )

    def record_usage(self, *args, **kwargs) -> None:
        """Usage accounting is not what these tests are about."""

    def monthly_usage(self, profile_id: str) -> int:
        """Quota enforcement (B.10) is not what these tests are about."""
        return 0

    def referral_counts(self, profile_id: str) -> ReferralCounts:
        """No referral bonus in play here either."""
        return ReferralCounts(referred_count=0, converted_count=0)


def _client(tmp_path):
    os.environ.pop("PMIE_AUTH_DISABLED", None)
    app = create_app(db_path=str(tmp_path / "sharing.db"))
    app.state.jwks = FakeJwks()
    app.state.accounts = FakeAccounts()
    app.state.sharing = FakeShareTokens()
    app.state.pipeline = AnalysisPipeline(
        app.state.registry, FakeRunner(), persistence=_FakePersistence()
    )
    return app, TestClient(app)


class _FakePersistence:
    """Always 'saves' and hands back an incrementing store id."""

    _next = 0

    def save(self, report, slug):
        _FakePersistence._next += 1
        return _FakePersistence._next


def _auth(user: str) -> dict:
    return {"authorization": f"Bearer {user}"}


def _create_analysis(client: TestClient, user: str) -> str:
    created = client.post(
        "/v1/analyses",
        json={"query": "why is world-cup-winner moving?"},
        headers=_auth(user),
    )
    assert created.status_code == 201, created.text
    return created.json()["analysis_id"]


class TestOwnership:
    def test_owner_can_read_their_own_report(self, tmp_path):
        _, client = _client(tmp_path)
        analysis_id = _create_analysis(client, OWNER)

        response = client.get(f"/v1/analyses/{analysis_id}", headers=_auth(OWNER))

        assert response.status_code == 200
        assert response.json()["status"] == "completed"

    def test_another_signed_in_user_cannot_read_it(self, tmp_path):
        _, client = _client(tmp_path)
        analysis_id = _create_analysis(client, OWNER)

        response = client.get(f"/v1/analyses/{analysis_id}", headers=_auth(OTHER))

        assert response.status_code == 403

    def test_anonymous_caller_without_a_share_token_is_401(self, tmp_path):
        _, client = _client(tmp_path)
        analysis_id = _create_analysis(client, OWNER)

        response = client.get(f"/v1/analyses/{analysis_id}")

        assert response.status_code == 401

    def test_events_stream_is_owner_only_no_share_exception(self, tmp_path):
        _, client = _client(tmp_path)
        analysis_id = _create_analysis(client, OWNER)

        # Even with a minted, valid share token, the live stream refuses a
        # non-owner — sharing exposes the finished report, not a run in
        # progress belonging to someone else.
        share = client.post(
            f"/v1/analyses/{analysis_id}/share", headers=_auth(OWNER)
        ).json()
        assert "token" in share

        response = client.get(
            f"/v1/analyses/{analysis_id}/events",
            params={"share_token": share["token"]},
            headers=_auth(OTHER),
        )
        assert response.status_code == 403


class TestShareTokens:
    def test_a_valid_share_token_lets_an_anonymous_caller_read_the_report(
        self, tmp_path
    ):
        _, client = _client(tmp_path)
        analysis_id = _create_analysis(client, OWNER)

        share = client.post(f"/v1/analyses/{analysis_id}/share", headers=_auth(OWNER))
        assert share.status_code == 200
        token = share.json()["token"]
        assert share.json()["url"].endswith(f"share_token={token}")

        response = client.get(
            f"/v1/analyses/{analysis_id}", params={"share_token": token}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"

    def test_only_the_owner_can_mint_a_share_token(self, tmp_path):
        _, client = _client(tmp_path)
        analysis_id = _create_analysis(client, OWNER)

        response = client.post(
            f"/v1/analyses/{analysis_id}/share", headers=_auth(OTHER)
        )

        assert response.status_code == 403

    def test_revoking_the_link_stops_anonymous_access(self, tmp_path):
        _, client = _client(tmp_path)
        analysis_id = _create_analysis(client, OWNER)

        token = client.post(
            f"/v1/analyses/{analysis_id}/share", headers=_auth(OWNER)
        ).json()["token"]

        revoke = client.delete(
            f"/v1/analyses/{analysis_id}/share", headers=_auth(OWNER)
        )
        assert revoke.status_code == 204

        response = client.get(
            f"/v1/analyses/{analysis_id}", params={"share_token": token}
        )
        assert response.status_code == 401

    def test_garbage_share_token_does_not_grant_access(self, tmp_path):
        _, client = _client(tmp_path)
        analysis_id = _create_analysis(client, OWNER)

        response = client.get(
            f"/v1/analyses/{analysis_id}",
            params={"share_token": "not-a-real-token"},
        )
        assert response.status_code == 401

    def test_a_token_for_one_report_does_not_open_another(self, tmp_path):
        """A share token names one report, not "some report".

        Resolving a token to *any* live row rather than to *this* row reads as
        a working share link in every other test here — each of them uses a
        single analysis — while letting anyone holding one valid token read
        every report in the store. The cross-report case is the only one that
        can tell those two implementations apart.
        """
        _, client = _client(tmp_path)
        shared = _create_analysis(client, OWNER)
        private = _create_analysis(client, OWNER)

        token = client.post(
            f"/v1/analyses/{shared}/share", headers=_auth(OWNER)
        ).json()["token"]

        # The token opens the report it was minted for.
        assert (
            client.get(
                f"/v1/analyses/{shared}", params={"share_token": token}
            ).status_code
            == 200
        )
        # And nothing else.
        assert (
            client.get(
                f"/v1/analyses/{private}", params={"share_token": token}
            ).status_code
            == 401
        )

    def test_creating_a_new_link_revokes_the_old_one(self, tmp_path):
        _, client = _client(tmp_path)
        analysis_id = _create_analysis(client, OWNER)

        first = client.post(
            f"/v1/analyses/{analysis_id}/share", headers=_auth(OWNER)
        ).json()["token"]
        second = client.post(
            f"/v1/analyses/{analysis_id}/share", headers=_auth(OWNER)
        ).json()["token"]
        assert first != second

        stale = client.get(f"/v1/analyses/{analysis_id}", params={"share_token": first})
        assert stale.status_code == 401

        fresh = client.get(
            f"/v1/analyses/{analysis_id}", params={"share_token": second}
        )
        assert fresh.status_code == 200


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("PMIE_AUTH_DISABLED", raising=False)
