"""TOTP via Supabase MFA — ROADMAP B.7.

Enrolling, challenging and verifying a TOTP factor all act on the *caller's
own* Supabase session. Unlike accounts.py/growth.py/sharing.py — which use
the service role key to act as an admin against Postgres's REST surface —
these calls go to Supabase's Auth server (GoTrue) with the caller's own
access token as `Authorization`, because GoTrue derives "which user" from
that token. `apikey` still carries the service role key: Supabase's gateway
accepts either an anon or a service key there, and Cygnus was deliberately
never given the publishable key (see .env.example) — reusing the service
role key needs nothing new.

Recovery codes are NOT built here. ROADMAP B.7 says "enroll / challenge /
verify + recovery codes", but Supabase Auth's MFA API has no recovery-code
concept at all — only enroll, challenge, verify, unenroll, listFactors and
getAuthenticatorAssuranceLevel. Hand-rolling a parallel recovery-code system
(a new table, our own hashing, our own redemption path) was prototyped and
then deliberately backed out: it's a scope decision for the user, not
something to build silently on an assumption. See WAVE1_LEDGER.md's Lane
notes / Follow-ups. `MfaEnrollResponse.recovery_codes` — frozen, so its shape
can't change here — is returned as an empty list until that's decided.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import supabase_secret_key, supabase_url

logger = logging.getLogger("cygnus.api.mfa")


class MfaError(Exception):
    """Any failure talking to Supabase Auth."""


class InvalidCode(MfaError):
    """The caller presented a TOTP code Supabase Auth rejected.

    Distinct from MfaError so the route can tell "you typed the wrong
    number" (422, the caller's problem) from "we couldn't reach Supabase"
    (503, ours) — collapsing them would report our own outage as a typo.
    """


@dataclass(frozen=True)
class EnrollResult:
    factor_id: str
    secret: str
    qr_uri: str


@dataclass(frozen=True)
class FactorStatus:
    enrolled: bool
    verified_at: str | None


class SupabaseMfa:
    def __init__(self, base_url: str | None = None, service_key: str | None = None):
        self._base_url = (base_url or supabase_url()).rstrip("/")
        self._service_key = service_key or supabase_secret_key()

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._service_key)

    def _request(
        self, method: str, path: str, user_token: str, **kwargs
    ) -> httpx.Response:
        if not self.configured:
            raise MfaError("Supabase is not configured")
        headers = {
            "apikey": self._service_key,
            "authorization": f"Bearer {user_token}",
            "content-type": "application/json",
            **kwargs.pop("headers", {}),
        }
        try:
            return httpx.request(
                method,
                f"{self._base_url}/auth/v1{path}",
                headers=headers,
                timeout=15,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise MfaError(f"Supabase Auth unreachable: {exc}") from exc

    def enroll(self, user_token: str) -> EnrollResult:
        response = self._request(
            "POST",
            "/factors",
            user_token,
            json={"factor_type": "totp", "friendly_name": "PMIE"},
        )
        if response.status_code >= 400:
            raise MfaError(
                f"enroll failed: {response.status_code} {response.text[:200]}"
            )
        body = response.json()
        totp = body.get("totp") or {}
        factor_id, secret, qr_uri = body.get("id"), totp.get("secret"), totp.get("uri")
        if not factor_id or not secret or not qr_uri:
            raise MfaError(f"enroll reply missing expected fields: {body!r}")
        return EnrollResult(factor_id=factor_id, secret=secret, qr_uri=qr_uri)

    def verify(self, user_token: str, factor_id: str, code: str) -> None:
        """Confirm a just-enrolled factor with a TOTP code.

        Two round trips because that is Supabase's own protocol: a challenge
        must exist before a code can be checked against it, so a stale or
        replayed code can't be verified without a fresh challenge in play.
        """
        challenge = self._request(
            "POST", f"/factors/{factor_id}/challenge", user_token, json={}
        )
        if challenge.status_code >= 400:
            raise MfaError(
                f"challenge failed: {challenge.status_code} {challenge.text[:200]}"
            )
        challenge_id = challenge.json().get("id")
        if not challenge_id:
            raise MfaError(f"challenge reply missing id: {challenge.text[:200]}")

        result = self._request(
            "POST",
            f"/factors/{factor_id}/verify",
            user_token,
            json={"challenge_id": challenge_id, "code": code},
        )
        if result.status_code < 300:
            return
        if result.status_code in (400, 422):
            raise InvalidCode("incorrect or expired code")
        raise MfaError(f"verify failed: {result.status_code} {result.text[:200]}")

    def status(self, user_token: str) -> FactorStatus:
        response = self._request("GET", "/user", user_token)
        if response.status_code >= 400:
            raise MfaError(
                f"could not load factors: {response.status_code} {response.text[:200]}"
            )
        factors = response.json().get("factors") or []
        verified = next(
            (
                f
                for f in factors
                if f.get("factor_type") == "totp" and f.get("status") == "verified"
            ),
            None,
        )
        if verified is None:
            return FactorStatus(enrolled=False, verified_at=None)
        # Supabase has no explicit "verified_at" on a factor; the last update
        # to a factor that is now verified is that verification, in practice.
        return FactorStatus(enrolled=True, verified_at=verified.get("updated_at"))


__all__ = [
    "EnrollResult",
    "FactorStatus",
    "InvalidCode",
    "MfaError",
    "SupabaseMfa",
]
