"""Growth surface: pre-signup capture (B.15) and product telemetry (B.19).

Talks to Supabase's PostgREST surface with the service role key, the same
transport `accounts.py` uses and for the same reason: the handful of calls
made here are simple, so a plain httpx client keeps the dependency surface
and the failure modes small.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import supabase_secret_key, supabase_url


class GrowthError(Exception):
    """Any failure talking to the growth store."""


@dataclass(frozen=True)
class WaitlistJoinResult:
    already_registered: bool


class Growth:
    def __init__(self, base_url: str | None = None, service_key: str | None = None):
        self._base_url = (base_url or supabase_url()).rstrip("/")
        self._service_key = service_key or supabase_secret_key()

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._service_key)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self.configured:
            raise GrowthError("Supabase is not configured")
        headers = {
            "apikey": self._service_key,
            "authorization": f"Bearer {self._service_key}",
            "content-type": "application/json",
            **kwargs.pop("headers", {}),
        }
        try:
            return httpx.request(
                method,
                f"{self._base_url}/rest/v1{path}",
                headers=headers,
                timeout=15,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise GrowthError(f"growth store unreachable: {exc}") from exc

    def join_waitlist(
        self, email: str, referral_code: str | None
    ) -> WaitlistJoinResult:
        """Insert one waitlist row, idempotently on email.

        Uniqueness is the database's job — the case-insensitive index on
        lower(email) in migrations/2026-09-07-beta-schema.sql — not
        application logic, so two concurrent signups for the same address
        can't both land as distinct rows. PostgREST reports that conflict as
        409, which this treats as the same success the caller got the first
        time, not an error: from the visitor's side, submitting their email
        twice isn't a failure.
        """
        response = self._request(
            "POST",
            "/waitlist",
            json={
                "email": email,
                "referral_code": referral_code,
                "source": "landing",
            },
            headers={"prefer": "return=minimal"},
        )
        if response.status_code == 409:
            return WaitlistJoinResult(already_registered=True)
        if response.status_code >= 400:
            raise GrowthError(
                f"growth store returned {response.status_code}: {response.text[:200]}"
            )
        return WaitlistJoinResult(already_registered=False)

    def record_event(
        self,
        profile_id: str,
        name: str,
        ui_mode: str | None,
        properties: dict[str, str],
    ) -> None:
        """Insert one product-telemetry row (B.19: mode switches, analyses started).

        `ui_mode` is a plain string here rather than the `UiMode` enum in
        models.py — the route layer already validated it against the
        contract, and this module has no other reason to depend on the API
        schema types.
        """
        response = self._request(
            "POST",
            "/user_events",
            json={
                "profile_id": profile_id,
                "name": name,
                "ui_mode": ui_mode,
                "properties": properties,
            },
        )
        if response.status_code >= 400:
            raise GrowthError(
                f"growth store returned {response.status_code}: {response.text[:200]}"
            )

    def record_pay_intent(
        self,
        profile_id: str,
        price_shown_usd: float,
        plan: str,
        list_price_usd: float,
    ) -> None:
        """One user_events row for a quota-wall click (B.10).

        No money moves during beta — see PayIntentRequest. Reuses user_events
        rather than a dedicated table: this is a product event like any
        other, just with its price/plan carried in properties. `list_price_usd`
        is the server's own PRO_MONTHLY_PRICE_USD, recorded next to whatever
        the client claims it showed, so the two can be compared later.
        """
        response = self._request(
            "POST",
            "/user_events",
            json={
                "profile_id": profile_id,
                "name": "pay_intent_clicked",
                "ui_mode": None,
                "properties": {
                    "price_shown_usd": str(price_shown_usd),
                    "list_price_usd": str(list_price_usd),
                    "plan": plan,
                },
            },
        )
        if response.status_code >= 400:
            raise GrowthError(
                f"growth store returned {response.status_code}: {response.text[:200]}"
            )
