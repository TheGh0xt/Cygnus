"""Reading Supabase configuration from the environment.

Supabase's own "Connect to your project" panel hands you these names:

    SUPABASE_URL
    SUPABASE_PUBLISHABLE_KEY
    SUPABASE_SECRET_KEY
    SUPABASE_JWKS_URL

Copying that block into a deployment is the obvious thing to do, so the
secret is accepted under Supabase's name as well as the one this codebase
started with. Insisting on our own spelling would mean a correctly-copied
configuration silently produces an unconfigured server — and the symptom is
a 503 from an unrelated endpoint, which is a miserable thing to debug.

Values are stripped: copy-paste from a dashboard routinely brings a trailing
newline or space, and an API key with whitespace on the end fails
authentication in a way that looks like a wrong key.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("cygnus.api.config")

# Ours first for backwards compatibility, then Supabase's own name.
_SECRET_KEY_VARS = ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY")


def _clean(value: str | None) -> str:
    return (value or "").strip()


def supabase_url() -> str:
    return _clean(os.getenv("SUPABASE_URL")).rstrip("/")


def supabase_secret_key() -> str:
    """The privileged key, under whichever name it was set.

    `sb_secret_...` is the modern replacement for the legacy `service_role`
    JWT; both work, and both must stay server-side.
    """
    for name in _SECRET_KEY_VARS:
        value = _clean(os.getenv(name))
        if value:
            if name != _SECRET_KEY_VARS[0]:
                logger.info("using %s for the Supabase secret key", name)
            return value
    return ""


def describe_supabase_config() -> dict:
    """A safe summary for diagnostics — never the key itself."""
    key = supabase_secret_key()
    return {
        "url_set": bool(supabase_url()),
        "secret_key_set": bool(key),
        # Enough to tell a publishable key from a secret one at a glance,
        # which is the mistake worth catching, without exposing the value.
        "secret_key_prefix": key[:11] if key else None,
        "checked_names": list(_SECRET_KEY_VARS),
    }
