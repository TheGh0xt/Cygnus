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
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from .config import supabase_secret_key, supabase_url
from .usage import TokenUsage

logger = logging.getLogger("cygnus.api.accounts")

# A user picks between this many interest categories at onboarding. Enforced
# here rather than in the database so a partial selection can still be saved
# while the user is mid-flow.
MIN_INTERESTS = 3
MAX_INTERESTS = 5

# The free allowance, enforced in routes.py's create_analysis (B.10).
FREE_MONTHLY_ANALYSES = 5

# B.10: converted referrals needed for one bonus grant, and the size of that
# grant. Product decision 2026-09-15: 6 converted referrals grant 3 bonus
# analyses (the initial 5-for-5 default was never a documented figure).
REFERRAL_CONVERSIONS_PER_BONUS = 6
REFERRAL_BONUS_ANALYSES = 3

# Beta-gate Pro price, text only — no billing integration exists yet. Shown in
# the quota-exceeded error and recorded alongside billing-intent clicks.
PRO_MONTHLY_PRICE_USD = 19.0

# Excludes 0/O/1/I so a code read off a screen can't be misdialled.
_REFERRAL_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_REFERRAL_CODE_LENGTH = 8


def bonus_analyses_for(converted_count: int) -> int:
    """Bonus analyses earned so far: REFERRAL_BONUS_ANALYSES for every

    complete group of REFERRAL_CONVERSIONS_PER_BONUS converted referrals.
    """
    return (converted_count // REFERRAL_CONVERSIONS_PER_BONUS) * REFERRAL_BONUS_ANALYSES


def next_reward_at(converted_count: int) -> int:
    """The converted-referral count at which the next bonus grant lands."""
    return (
        converted_count // REFERRAL_CONVERSIONS_PER_BONUS + 1
    ) * REFERRAL_CONVERSIONS_PER_BONUS


def effective_allowance(converted_referral_count: int) -> int:
    """This month's real allowance: the free tier plus any referral bonus."""
    return FREE_MONTHLY_ANALYSES + bonus_analyses_for(converted_referral_count)


class AccountsError(Exception):
    """Any failure talking to the account store."""


@dataclass(frozen=True)
class ReferralCounts:
    referred_count: int
    converted_count: int


@dataclass(frozen=True)
class Profile:
    id: str
    display_name: str | None
    is_invited: bool
    is_grandfathered: bool
    onboarding_completed_at: str | None
    # B.19: NULL means "never chose" (see migrations/2026-09-07-beta-schema.sql).
    ui_mode: str | None = None
    referral_code: str | None = None
    referred_by: str | None = None


class Accounts:
    def __init__(self, base_url: str | None = None, service_key: str | None = None):
        self._base_url = (base_url or supabase_url()).rstrip("/")
        self._service_key = service_key or supabase_secret_key()

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._service_key)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        return self._request_allow(method, path, **kwargs)

    def _request_allow(
        self, method: str, path: str, ok_extra: tuple[int, ...] = (), **kwargs
    ) -> httpx.Response:
        """Like `_request`, but a status in `ok_extra` is returned, not raised.

        Some writes here are expected to occasionally collide — a referral
        already attributed (409), a referral-code guess someone else just
        took (409) — and that is the caller's normal path, not a failure.
        """
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
        if response.status_code >= 400 and response.status_code not in ok_extra:
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
            ui_mode=row.get("ui_mode"),
            referral_code=row.get("referral_code"),
            referred_by=row.get("referred_by"),
        )

    def mark_onboarded(self, profile_id: str) -> None:
        self._request(
            "PATCH",
            "/profiles",
            params={"id": f"eq.{profile_id}"},
            json={"onboarding_completed_at": "now()", "updated_at": "now()"},
        )

    def set_ui_mode(self, profile_id: str, ui_mode: str) -> None:
        """Persist the caller's UI-mode choice (B.19), so it survives a new
        device or session rather than resetting on every login."""
        self._request(
            "PATCH",
            "/profiles",
            params={"id": f"eq.{profile_id}"},
            json={"ui_mode": ui_mode, "updated_at": "now()"},
        )

    # ---- referrals (B.10) ---------------------------------------------

    def get_referral_code(self, profile_id: str) -> str:
        """This user's referral code, assigning one on first use.

        Nothing else in this codebase writes profiles.referral_code at
        signup, so this assigns it lazily the first time it's asked for
        rather than depend on that happening somewhere else.
        """
        profile = self.get_profile(profile_id)
        if profile is None:
            raise AccountsError(f"no profile for {profile_id}")
        if profile.referral_code:
            return profile.referral_code
        return self._assign_referral_code(profile_id)

    def _assign_referral_code(self, profile_id: str, attempts: int = 5) -> str:
        """Assign a code with a single conditional write, not check-then-set.

        The prior check-then-PATCH had a TOCTOU gap: two concurrent first
        reads could each pass the "no code yet" check and PATCH a different
        code, with the second silently overwriting the first. The PATCH is
        now conditional on `referral_code=is.null`, so at most one concurrent
        caller can win it.
        """
        for _ in range(attempts):
            code = "".join(
                secrets.choice(_REFERRAL_CODE_ALPHABET)
                for _ in range(_REFERRAL_CODE_LENGTH)
            )
            response = self._request_allow(
                "PATCH",
                "/profiles",
                ok_extra=(409,),
                params={"id": f"eq.{profile_id}", "referral_code": "is.null"},
                json={"referral_code": code, "updated_at": "now()"},
                headers={"prefer": "return=representation"},
            )
            if response.status_code == 409:
                # Our own guessed code collided with someone else's row —
                # try a new random guess, not a retry of the same one.
                continue
            rows = response.json()
            if rows:
                return rows[0]["referral_code"]
            # 0 rows back: the conditional match failed, meaning a concurrent
            # request already set referral_code first. Read what it set.
            profile = self.get_profile(profile_id)
            if profile is None or not profile.referral_code:
                raise AccountsError(f"no profile for {profile_id}")
            return profile.referral_code
        raise AccountsError("could not generate a unique referral code")

    def referral_counts(self, profile_id: str) -> ReferralCounts:
        return ReferralCounts(
            referred_count=self._count_rows(
                "/referrals", {"referrer_profile_id": f"eq.{profile_id}"}
            ),
            converted_count=self._count_rows(
                "/referrals",
                {
                    "referrer_profile_id": f"eq.{profile_id}",
                    "converted_at": "not.is.null",
                },
            ),
        )

    def sync_referral_attribution(self, profile: Profile, email: str | None) -> None:
        """Attribute this signup to a referrer, the first time it's checked.

        Nothing writes `referrals` at signup: the referral code a visitor
        signed up under is only recorded on their waitlist row. Called from
        `/me` on every load until `profile.referred_by` is set, so it costs
        two reads once attribution has already happened. Best-effort, like
        `record_usage` — a failure here must not fail `/me`.
        """
        if profile.referred_by is not None or not email:
            return
        try:
            waitlist_rows = self._request(
                "GET",
                "/waitlist",
                params={
                    "email": f"eq.{email}",
                    "referral_code": "not.is.null",
                    "select": "referral_code",
                    "limit": 1,
                },
            ).json()
            if not waitlist_rows:
                return
            referral_code = waitlist_rows[0]["referral_code"]

            referrer_rows = self._request(
                "GET",
                "/profiles",
                params={
                    "referral_code": f"eq.{referral_code}",
                    "select": "id",
                    "limit": 1,
                },
            ).json()
            if not referrer_rows:
                return
            referrer_id = referrer_rows[0]["id"]
            if referrer_id == profile.id:
                return  # self-referral: the code happened to be their own

            # 409 from referrals_referred_once means another call already
            # attributed this profile — already done, not an error.
            self._request_allow(
                "POST",
                "/referrals",
                ok_extra=(409,),
                json={
                    "referrer_profile_id": referrer_id,
                    "referred_profile_id": profile.id,
                },
                headers={"prefer": "return=minimal"},
            )
            self._request(
                "PATCH",
                "/profiles",
                params={"id": f"eq.{profile.id}"},
                json={"referred_by": referrer_id, "updated_at": "now()"},
            )
        except AccountsError:
            logger.exception(
                "failed to sync referral attribution for %s", profile.id
            )

    def mark_referral_converted(self, profile_id: str) -> None:
        """Mark this referred user's row converted, once their email is verified.

        A conditional PATCH (`converted_at=is.null`) rather than a full
        upsert: it is a no-op both when this profile was never referred and
        when it was already marked converted, so calling this on every
        verified `/me` load is safe. Best-effort, same posture as
        `record_usage`.
        """
        try:
            self._request(
                "PATCH",
                "/referrals",
                params={
                    "referred_profile_id": f"eq.{profile_id}",
                    "converted_at": "is.null",
                },
                json={"converted_at": "now()"},
            )
        except AccountsError:
            logger.exception("failed to mark referral converted for %s", profile_id)

    def _count_rows(self, path: str, params: dict) -> int:
        response = self._request(
            "GET",
            path,
            params={**params, "select": "id"},
            headers={"prefer": "count=exact", "range-unit": "items", "range": "0-0"},
        )
        content_range = response.headers.get("content-range", "")
        total = content_range.split("/")[-1] if "/" in content_range else "0"
        return int(total) if total.isdigit() else 0

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
        tokens: TokenUsage | None = None,
    ) -> None:
        """Record one analysis attempt.

        Never raises into the caller's path: usage accounting must not be able
        to fail a request the user already paid attention to. A dropped row is
        a reporting gap; a failed analysis is a broken product.

        That tolerance also covers the token columns (ROADMAP 5.6) before
        their migration has been applied: PostgREST rejects unknown columns,
        the rejection is caught here, and analyses keep working while the cost
        record stays empty. Check the logs after deploying the migration
        rather than assuming silence means success — that assumption is
        exactly what cost five days on 2026-08-20.
        """
        payload = {
            "profile_id": profile_id,
            "analysis_id": analysis_id,
            "outcome": outcome,
            "event_slug": event_slug,
            "duration_ms": duration_ms,
        }
        # Omitted entirely when nothing was measured: a row of zeroes in a
        # cost record reads as an analysis that was free.
        if tokens:
            payload.update(tokens.as_dict())

        try:
            self._request(
                "POST",
                "/analysis_usage",
                json=payload,
            )
        except AccountsError:
            logger.exception("failed to record usage for analysis %s", analysis_id)

    def monthly_usage(self, profile_id: str) -> int:
        """Completed analyses so far this calendar month (UTC).

        Failed runs never count. B.10: this used to have no date filter at
        all, silently counting every completed analysis ever — a gap that
        went unnoticed because nothing compared the result to the allowance.
        """
        month_start = (
            datetime.now(UTC)
            .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )
        return self._count_rows(
            "/analysis_usage",
            {
                "profile_id": f"eq.{profile_id}",
                "outcome": "eq.completed",
                "created_at": f"gte.{month_start}",
            },
        )

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
