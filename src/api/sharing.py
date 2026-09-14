"""Share tokens — ROADMAP B.6.

A deliberately minted, public, read-only link for one completed report
(UI_PRD 6.7). This is why `GET /v1/analyses/{id}` is not simply
unauthenticated: sharing is an explicit act a signed-in owner takes, not an
accident of an unguessable id.

Same PostgREST-over-httpx shape as `Accounts` and the same reason for it: the
handful of calls here are simple enough that the Supabase SDK would be a
second dependency for no real benefit. Backed by the `share_tokens` table
from migrations/2026-09-07-beta-schema.sql.

One live link per report, not one per mint: creating a new token revokes any
existing one for that report first, so "the report's public link" (singular,
per UI_PRD) stays true instead of accumulating live tokens nobody can find to
revoke.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .config import supabase_secret_key, supabase_url

logger = logging.getLogger("cygnus.api.sharing")

# 30 days: long enough that a link shared once still works when someone
# opens it later, short enough that a link is not effectively permanent.
DEFAULT_TTL_DAYS = 30


class SharingError(Exception):
    """Any failure talking to the share-token store."""


@dataclass(frozen=True)
class ShareToken:
    token: str
    report_id: int
    expires_at: datetime | None


class ShareTokens:
    def __init__(self, base_url: str | None = None, service_key: str | None = None):
        self._base_url = (base_url or supabase_url()).rstrip("/")
        self._service_key = service_key or supabase_secret_key()

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._service_key)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self.configured:
            raise SharingError("Supabase is not configured")
        headers = {
            "apikey": self._service_key,
            "authorization": f"Bearer {self._service_key}",
            "content-type": "application/json",
            **kwargs.pop("headers", {}),
        }
        try:
            response = httpx.request(
                method,
                f"{self._base_url}/rest/v1{path}",
                headers=headers,
                timeout=15,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise SharingError(f"share token store unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise SharingError(
                f"share token store returned {response.status_code}: "
                f"{response.text[:200]}"
            )
        return response

    def create(
        self,
        report_id: int,
        created_by: str,
        ttl_days: int = DEFAULT_TTL_DAYS,
    ) -> ShareToken:
        now = datetime.now(tz=UTC)
        expires_at = now + timedelta(days=ttl_days) if ttl_days else None

        # Revoke first: a report has one live link at a time.
        self._request(
            "PATCH",
            "/share_tokens",
            params={"report_id": f"eq.{report_id}", "revoked_at": "is.null"},
            json={"revoked_at": now.isoformat()},
        )

        token = secrets.token_urlsafe(24)
        self._request(
            "POST",
            "/share_tokens",
            json={
                "token": token,
                "report_id": report_id,
                "created_by": created_by,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
        )
        return ShareToken(token=token, report_id=report_id, expires_at=expires_at)

    def resolve(self, token: str) -> int | None:
        """The report_id a live token points at, or None.

        None covers "no such token", "revoked" and "expired" identically —
        a caller checking access needs only yes/no, and distinguishing the
        reasons would let someone probe for which tokens once existed.
        """
        if not token or not self.configured:
            return None
        try:
            rows = self._request(
                "GET",
                "/share_tokens",
                params={
                    "token": f"eq.{token}",
                    "revoked_at": "is.null",
                    "select": "report_id,expires_at",
                    "limit": 1,
                },
            ).json()
        except SharingError:
            logger.exception("could not resolve share token")
            return None

        if not rows:
            return None
        row = rows[0]
        expires_at = row.get("expires_at")
        if expires_at and _parse_ts(expires_at) <= datetime.now(tz=UTC):
            return None
        return int(row["report_id"])

    def revoke(self, report_id: int, revoked_by: str) -> None:
        """Revoke every live token for a report.

        `revoked_by` is accepted for a future audit column and is not sent
        today — the schema only tracks who created a link, not who revoked
        it. Kept in the signature so the caller (routes.py) does not need to
        change when that column is added.
        """
        del revoked_by
        self._request(
            "PATCH",
            "/share_tokens",
            params={"report_id": f"eq.{report_id}", "revoked_at": "is.null"},
            json={"revoked_at": datetime.now(tz=UTC).isoformat()},
        )


def _parse_ts(value: str) -> datetime:
    if value.endswith("+00"):
        value = value[:-3] + "+00:00"
    return datetime.fromisoformat(value)


__all__ = ["DEFAULT_TTL_DAYS", "ShareToken", "ShareTokens", "SharingError"]
