"""Request dependencies: identity and access.

Kept separate from routes.py so the auth rules can be read — and changed — in
one place rather than being scattered across endpoint signatures.
"""

from __future__ import annotations

import logging

from fastapi import Request

from .accounts import AccountsError, Profile
from .auth import AuthError, CurrentUser, extract_bearer_token
from .errors import ErrorType, PmieError

logger = logging.getLogger("cygnus.api.dependencies")


def current_user(request: Request) -> CurrentUser:
    """The verified caller, or a 401.

    Auth can be disabled for local development via PMIE_AUTH_DISABLED, which
    is why that flag is read from app state set at startup rather than from
    the environment here — a stray env var must not be able to switch off
    authentication in a deployed process.
    """
    if getattr(request.app.state, "auth_disabled", False):
        return CurrentUser(id="00000000-0000-0000-0000-000000000000", email=None)

    try:
        token = extract_bearer_token(request.headers.get("authorization"))
        return request.app.state.jwks.verify(token)
    except AuthError as exc:
        raise PmieError(
            ErrorType.INVALID_REQUEST, "Sign in to continue.", status=401
        ) from exc
    except Exception as exc:
        # A JWKS fetch failure is our problem, not the caller's, and must not
        # be reported as bad credentials.
        logger.exception("could not verify token")
        raise PmieError(
            ErrorType.INTERNAL_ERROR,
            "Could not verify your session. Try again shortly.",
            status=503,
        ) from exc


def require_invited(request: Request, user: CurrentUser) -> Profile:
    """Gate analysis behind the invite list.

    The alpha is invite-only, and a profile row exists from the moment a user
    signs up — so signing up is allowed, running analyses is not, until the
    invite flag is set.
    """
    # The dev bypass has to cover the invite gate too. Otherwise disabling
    # auth locally still fails every analysis with "accounts not configured",
    # which is a confusing way to say "you have no database".
    if getattr(request.app.state, "auth_disabled", False):
        return Profile(
            id=user.id,
            display_name="local",
            is_invited=True,
            is_grandfathered=False,
            onboarding_completed_at=None,
        )

    accounts = request.app.state.accounts
    if not accounts.configured:
        raise PmieError(
            ErrorType.INTERNAL_ERROR,
            "Accounts are not configured on this server.",
            status=503,
        )

    try:
        profile = accounts.get_profile(user.id)
    except AccountsError as exc:
        raise PmieError(
            ErrorType.INTERNAL_ERROR,
            "Could not load your account. Try again shortly.",
            status=503,
        ) from exc

    if profile is None:
        raise PmieError(
            ErrorType.INVALID_REQUEST, "No account found for this session.", status=401
        )

    if not profile.is_invited:
        raise PmieError(
            ErrorType.QUOTA_EXCEEDED,
            "PMIE is invite-only during the private alpha. "
            "Your account is on the waitlist.",
            status=403,
        )

    return profile
