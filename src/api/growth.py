"""Growth surface: pre-signup capture — ROADMAP B.15.

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
