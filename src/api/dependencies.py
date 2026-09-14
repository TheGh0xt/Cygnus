"""Request dependencies: identity and access.

Kept separate from routes.py so the auth rules can be read — and changed — in
one place rather than being scattered across endpoint signatures.

Identity itself (verifying the bearer token, or rejecting its absence) now
lives in access.py as a fail-closed `Depends` — see that module's docstring.
What remains here is *authorization*: rules that need a profile, not just a
verified token.
"""

from __future__ import annotations

import logging

from fastapi import Request

from .accounts import AccountsError, Profile
from .auth import CurrentUser
from .errors import ErrorType, PmieError

logger = logging.getLogger("cygnus.api.dependencies")


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
