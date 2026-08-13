"""Supabase JWT verification.

Supabase signs access tokens with ES256 and publishes the public half at a
JWKS endpoint, so tokens are verified locally against a cached public key.
There is no shared secret to leak, and no network round trip per request.

The browser authenticates against Supabase directly and sends the resulting
token here. Cygnus never sees a password, and the Gemini and MCP credentials
never leave this process.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
import jwt
from jwt import PyJWKClient

from .config import supabase_url as _supabase_url

logger = logging.getLogger("cygnus.api.auth")

# Supabase issues tokens with this audience for signed-in users.
_AUDIENCE = "authenticated"

# Only asymmetric signing is accepted. Listing algorithms explicitly is what
# stops a forged token from choosing its own — notably alg=none, and the
# HS256 confusion attack where a public key is used as an HMAC secret.
_ALGORITHMS = ["ES256"]


class AuthError(Exception):
    """Raised for any token that cannot be trusted.

    Deliberately carries no detail about *why*. Telling a caller whether a
    token was expired, forged or malformed helps an attacker more than it
    helps a legitimate client, which only ever needs to re-authenticate.
    """


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str | None


def supabase_url() -> str:
    url = _supabase_url()
    if not url:
        raise RuntimeError("SUPABASE_URL is not set")
    return url


def extract_bearer_token(header: str | None) -> str:
    if not header:
        raise AuthError("missing authorization header")
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError("authorization header is not a bearer token")
    token = parts[1].strip()
    if not token:
        raise AuthError("empty bearer token")
    return token


def decode_token(token: str, jwks: dict) -> CurrentUser:
    """Verify a token against a JWKS document and return the user.

    Takes the JWKS as an argument rather than fetching it so the verification
    logic is testable without a network or a live Supabase project.
    """
    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:
        raise AuthError("malformed token") from exc

    kid = header.get("kid")
    key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if key_data is None:
        raise AuthError("token signed by an unknown key")

    try:
        key = jwt.PyJWK(key_data).key
        claims = jwt.decode(
            token,
            key=key,
            algorithms=_ALGORITHMS,
            audience=_AUDIENCE,
            options={"require": ["exp", "sub"]},
        )
    except Exception as exc:
        raise AuthError("token failed verification") from exc

    subject = claims.get("sub")
    if not subject:
        raise AuthError("token has no subject")

    return CurrentUser(id=str(subject), email=claims.get("email"))


class JwksCache:
    """Fetches and caches the signing keys.

    Supabase rotates keys rarely, so a long TTL is fine; on an unknown key id
    the cache refreshes once before rejecting, which lets a rotation take
    effect without a restart or a window of failed logins.
    """

    def __init__(self, url: str | None = None, ttl_seconds: int = 3600):
        self._url = url
        self._ttl = ttl_seconds
        self._jwks: dict | None = None
        self._fetched_at = 0.0

    @property
    def url(self) -> str:
        return self._url or f"{supabase_url()}/auth/v1/.well-known/jwks.json"

    def get(self, force_refresh: bool = False) -> dict:
        fresh = time.time() - self._fetched_at < self._ttl
        if self._jwks is not None and fresh and not force_refresh:
            return self._jwks
        response = httpx.get(self.url, timeout=10)
        response.raise_for_status()
        self._jwks = response.json()
        self._fetched_at = time.time()
        return self._jwks

    def verify(self, token: str) -> CurrentUser:
        try:
            return decode_token(token, self.get())
        except AuthError:
            # Retry once against freshly-fetched keys, in case this is simply
            # a token signed by a key we have not seen yet.
            return decode_token(token, self.get(force_refresh=True))


__all__ = [
    "AuthError",
    "CurrentUser",
    "JwksCache",
    "PyJWKClient",
    "decode_token",
    "extract_bearer_token",
    "supabase_url",
]
