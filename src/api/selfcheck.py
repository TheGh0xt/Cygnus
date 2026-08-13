"""Startup verification that this process can actually write.

The failure this exists to catch: configure Cygnus with Supabase's
*publishable* key instead of the secret one and everything looks healthy.
Reads succeed — tables with a public policy return rows, tables without one
return an empty list rather than an error — while every write is refused by
row level security.

The result is a server that serves pages, answers health checks, runs
analyses, and silently discards every report. Nothing surfaces until someone
asks why the accuracy record is empty, by which point the reports are gone
and cannot be recreated: each one carries the market price observed at the
moment it was written.

So the check is a real write, not an inspection. Reading proves nothing here,
because reading is exactly what still works when the credentials are wrong.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from .config import describe_supabase_config

logger = logging.getLogger("cygnus.api.selfcheck")

# Supabase's secret keys carry this prefix. Legacy service-role credentials are
# JWTs and start with "eyJ", so both are accepted; a publishable key is the
# mistake worth naming explicitly.
_SECRET_PREFIXES = ("sb_secret_", "eyJ")
_PUBLISHABLE_PREFIX = "sb_publishable_"


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str

    @property
    def status(self) -> str:
        return "ok" if self.ok else "failed"


def inspect_key_shape() -> CheckResult:
    """Cheap check on the key's prefix, before spending a network round trip."""
    config = describe_supabase_config()
    if not config["url_set"]:
        return CheckResult(False, "SUPABASE_URL is not set")
    if not config["secret_key_set"]:
        return CheckResult(
            False,
            "No secret key set (SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SECRET_KEY)",
        )

    prefix = config["secret_key_prefix"] or ""
    if prefix.startswith(_PUBLISHABLE_PREFIX[:11]):
        return CheckResult(
            False,
            "A publishable key is configured where the secret key belongs. "
            "Reads will appear to work and every write will be refused.",
        )
    if not any(prefix.startswith(p[: len(prefix)]) for p in _SECRET_PREFIXES):
        return CheckResult(False, f"Unrecognised key format (starts {prefix!r})")
    return CheckResult(True, "key shape looks right")


def verify_write_access(accounts) -> CheckResult:
    """Insert a sentinel row and remove it again.

    A real write is the only honest test: row level security is enforced on
    writes and effectively invisible on reads.

    The sentinel goes into analysis_usage rather than analysis_reports because
    usage rows are disposable telemetry, and it is removed immediately. A
    leftover row would be visible in usage counts, so failure to clean up is
    reported rather than ignored.
    """
    if not getattr(accounts, "configured", False):
        return CheckResult(False, "Supabase is not configured")

    shape = inspect_key_shape()
    if not shape.ok:
        return shape

    probe_id = f"startup-probe-{uuid.uuid4().hex[:12]}"
    profile_id = "00000000-0000-0000-0000-000000000000"

    try:
        accounts._request(
            "POST",
            "/analysis_usage",
            json={
                "profile_id": profile_id,
                "analysis_id": probe_id,
                "outcome": "failed",
                "event_slug": None,
            },
        )
    except Exception as exc:  # noqa: BLE001 — a startup probe must never
        # take the process down, whatever the store does.
        message = str(exc)
        if "row-level security" in message or "42501" in message:
            return CheckResult(
                False,
                "Writes are refused by row level security. This is what a "
                "publishable key looks like in place of the secret key.",
            )
        if "foreign key" in message or "23503" in message:
            # The probe profile does not exist, which means the insert was
            # authorised and rejected on referential grounds — write access is
            # fine, which is all this check cares about.
            return CheckResult(True, "write access confirmed")
        return CheckResult(False, f"write probe failed: {message[:160]}")

    try:
        accounts._request(
            "DELETE", "/analysis_usage", params={"analysis_id": f"eq.{probe_id}"}
        )
    except Exception:  # noqa: BLE001 — cleanup failure is worth noting,
        # not worth failing a check that has already proved its point.
        logger.warning("startup probe row %s could not be removed", probe_id)
        return CheckResult(True, "write access confirmed (probe row left behind)")

    return CheckResult(True, "write access confirmed")


def run_startup_checks(accounts) -> CheckResult:
    """Verify write access and say so loudly if it is missing."""
    result = verify_write_access(accounts)
    if result.ok:
        logger.info("startup check: %s", result.detail)
        return result

    # Deliberately alarming. The alternative is a server that looks healthy
    # while throwing away the only data the product's claims rest on.
    logger.error(
        "STARTUP CHECK FAILED — this server cannot persist reports. %s "
        "Every completed analysis will be lost and cannot be recreated. "
        "Supabase config: %s",
        result.detail,
        describe_supabase_config(),
    )
    return result
