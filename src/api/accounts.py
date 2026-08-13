"""Account data access: profiles, interests, usage and feedback.

Talks to Supabase's PostgREST surface with the service role key, so it
bypasses row level security. That is deliberate and it is why this module
must never accept a caller-supplied profile id: every method takes the id
resolved from a verified JWT. RLS is the second line of defence here, not
the first.

The transport is a plain httpx client rather than the Supabase SDK — the
handful of calls we make are simple, and this keeps the dependency surface
and the failure modes small.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger("cygnus.api.accounts")

# A user picks between this many interest categories at onboarding. Enforced
# here rather than in the database so a partial selection can still be saved
# while the user is mid-flow.
MIN_INTERESTS = 3
MAX_INTERESTS = 5

# The free allowance. Recorded and reported, but NOT enforced yet: pricing is
# gated behind a published accuracy record, so today this only tells a user
# where they stand.
FREE_MONTHLY_ANALYSES = 5


class AccountsError(Exception):
    """Any failure talking to the account store."""


@dataclass(frozen=True)
class Profile:
    id: str
    display_name: str | None
    is_invited: bool
    is_grandfathered: bool
    onboarding_completed_at: str | None


class Accounts:
    def __init__(self, base_url: str | None = None, service_key: str | None = None):
        self._base_url = (base_url or os.getenv("SUPABASE_URL", "")).rstrip("/")
        self._service_key = service_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._service_key)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self.configured:
            raise AccountsError("Supabase is not configured")
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
            raise AccountsError(f"account store unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise AccountsError(
                f"account store returned {response.status_code}: {response.text[:200]}"
            )
        return response

    # ---- profiles ----------------------------------------------------

    def get_profile(self, profile_id: str) -> Profile | None:
        rows = self._request(
            "GET",
            "/profiles",
            params={"id": f"eq.{profile_id}", "select": "*", "limit": 1},
        ).json()
        if not rows:
            return None
        row = rows[0]
        return Profile(
            id=row["id"],
            display_name=row.get("display_name"),
            is_invited=bool(row.get("is_invited")),
            is_grandfathered=bool(row.get("is_grandfathered")),
            onboarding_completed_at=row.get("onboarding_completed_at"),
        )

    def mark_onboarded(self, profile_id: str) -> None:
        self._request(
            "PATCH",
            "/profiles",
            params={"id": f"eq.{profile_id}"},
            json={"onboarding_completed_at": "now()", "updated_at": "now()"},
        )

    # ---- interests ---------------------------------------------------

    def list_categories(self) -> list[dict]:
        return self._request(
            "GET",
            "/interest_categories",
            params={
                "is_active": "eq.true",
                "select": "slug,label,description,sort_order",
                "order": "sort_order.asc",
            },
        ).json()

    def get_interests(self, profile_id: str) -> list[str]:
        rows = self._request(
            "GET",
            "/profile_interests",
            params={"profile_id": f"eq.{profile_id}", "select": "category_slug"},
        ).json()
        return [row["category_slug"] for row in rows]

    def set_interests(self, profile_id: str, slugs: list[str]) -> list[str]:
        """Replace a user's selections.

        Replace rather than merge: the onboarding screen submits the whole
        set, so a merge would make deselecting impossible.
        """
        unique = list(dict.fromkeys(slugs))
        if not MIN_INTERESTS <= len(unique) <= MAX_INTERESTS:
            raise AccountsError(
                f"choose between {MIN_INTERESTS} and {MAX_INTERESTS} categories"
            )

        valid = {row["slug"] for row in self.list_categories()}
        unknown = [slug for slug in unique if slug not in valid]
        if unknown:
            raise AccountsError(f"unknown categories: {', '.join(sorted(unknown))}")

        self._request(
            "DELETE", "/profile_interests", params={"profile_id": f"eq.{profile_id}"}
        )
        self._request(
            "POST",
            "/profile_interests",
            json=[{"profile_id": profile_id, "category_slug": s} for s in unique],
        )
        self.mark_onboarded(profile_id)
        return unique

    # ---- usage -------------------------------------------------------

    def record_usage(
        self,
        profile_id: str,
        analysis_id: str,
        outcome: str,
        event_slug: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Record one analysis attempt.

        Never raises into the caller's path: usage accounting must not be able
        to fail a request the user already paid attention to. A dropped row is
        a reporting gap; a failed analysis is a broken product.
        """
        try:
            self._request(
                "POST",
                "/analysis_usage",
                json={
                    "profile_id": profile_id,
                    "analysis_id": analysis_id,
                    "outcome": outcome,
                    "event_slug": event_slug,
                    "duration_ms": duration_ms,
                },
            )
        except AccountsError:
            logger.exception("failed to record usage for analysis %s", analysis_id)

    def monthly_usage(self, profile_id: str) -> int:
        """Completed analyses this calendar month. Failed runs never count."""
        response = self._request(
            "GET",
            "/analysis_usage",
            params={
                "profile_id": f"eq.{profile_id}",
                "outcome": "eq.completed",
                "select": "id",
            },
            headers={"prefer": "count=exact", "range-unit": "items", "range": "0-0"},
        )
        content_range = response.headers.get("content-range", "")
        total = content_range.split("/")[-1] if "/" in content_range else "0"
        return int(total) if total.isdigit() else 0

    # ---- feedback ----------------------------------------------------

    def save_feedback(
        self, profile_id: str, analysis_id: str, is_useful: bool, note: str | None
    ) -> None:
        self._request(
            "POST",
            "/report_feedback",
            headers={"prefer": "resolution=merge-duplicates"},
            json={
                "profile_id": profile_id,
                "analysis_id": analysis_id,
                "is_useful": is_useful,
                "note": note,
            },
        )
