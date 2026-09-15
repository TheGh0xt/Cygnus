from datetime import UTC, datetime

import pytest

from src.api.accounts import (
    FREE_MONTHLY_ANALYSES,
    MAX_INTERESTS,
    MIN_INTERESTS,
    REFERRAL_BONUS_ANALYSES,
    REFERRAL_CONVERSIONS_PER_BONUS,
    Accounts,
    AccountsError,
    Profile,
    bonus_analyses_for,
    effective_allowance,
    next_reward_at,
)


class FakeAccounts(Accounts):
    """Accounts with the HTTP layer replaced, so the rules are testable."""

    def __init__(self):
        super().__init__(base_url="https://example.test", service_key="service-key")
        self.categories = [
            {"slug": "politics", "label": "Politics"},
            {"slug": "crypto", "label": "Crypto"},
            {"slug": "sports", "label": "Sports"},
            {"slug": "ai", "label": "AI"},
            {"slug": "climate", "label": "Climate"},
            {"slug": "health", "label": "Health"},
        ]
        self.interests: dict[str, list[str]] = {}
        self.onboarded: list[str] = []
        self.usage: list[dict] = []

    def list_categories(self):
        return self.categories

    def mark_onboarded(self, profile_id):
        self.onboarded.append(profile_id)

    def _request(self, method, path, **kwargs):  # pragma: no cover - guard
        raise AssertionError(f"unexpected HTTP call: {method} {path}")


def test_free_allowance_is_five():
    # Stated in the PRD and at the payment boundary; pinned so it cannot drift
    # silently away from what users were told.
    assert FREE_MONTHLY_ANALYSES == 5


def test_interest_range_is_three_to_five():
    assert (MIN_INTERESTS, MAX_INTERESTS) == (3, 5)


class TestSetInterests:
    def _accounts(self):
        accounts = FakeAccounts()

        stored: dict[str, list[str]] = {}

        def fake_request(method, path, **kwargs):
            if method == "DELETE":
                stored.clear()
            elif method == "POST":
                for row in kwargs["json"]:
                    stored.setdefault(row["profile_id"], []).append(
                        row["category_slug"]
                    )

            class R:
                headers: dict = {}

                @staticmethod
                def json():
                    return []

            return R()

        accounts._request = fake_request  # type: ignore[method-assign]
        accounts._stored = stored  # type: ignore[attr-defined]
        return accounts

    def test_accepts_three(self):
        accounts = self._accounts()
        result = Accounts.set_interests(accounts, "u1", ["politics", "crypto", "ai"])
        assert result == ["politics", "crypto", "ai"]

    def test_accepts_five(self):
        accounts = self._accounts()
        chosen = ["politics", "crypto", "ai", "sports", "climate"]
        assert Accounts.set_interests(accounts, "u1", chosen) == chosen

    def test_rejects_two(self):
        accounts = self._accounts()
        with pytest.raises(AccountsError, match="between 3 and 5"):
            Accounts.set_interests(accounts, "u1", ["politics", "crypto"])

    def test_rejects_six(self):
        accounts = self._accounts()
        with pytest.raises(AccountsError, match="between 3 and 5"):
            Accounts.set_interests(
                accounts,
                "u1",
                ["politics", "crypto", "ai", "sports", "climate", "health"],
            )

    def test_duplicates_collapse_before_the_count_is_checked(self):
        # Sending the same category twice is a client bug, not five choices.
        accounts = self._accounts()
        with pytest.raises(AccountsError, match="between 3 and 5"):
            Accounts.set_interests(
                accounts, "u1", ["politics", "politics", "crypto", "crypto"]
            )

    def test_rejects_unknown_categories(self):
        accounts = self._accounts()
        with pytest.raises(AccountsError, match="unknown categories"):
            Accounts.set_interests(accounts, "u1", ["politics", "crypto", "tarot"])

    def test_marks_onboarding_complete(self):
        accounts = self._accounts()
        Accounts.set_interests(accounts, "u1", ["politics", "crypto", "ai"])
        assert accounts.onboarded == ["u1"]


class TestRecordUsage:
    def test_a_store_failure_never_propagates(self):
        # Usage accounting must not be able to fail a request the user has
        # already waited two minutes for.
        accounts = FakeAccounts()

        def boom(*args, **kwargs):
            raise AccountsError("store down")

        accounts._request = boom  # type: ignore[method-assign]
        Accounts.record_usage(accounts, "u1", "a1", "completed")


class TestProfileUiMode:
    def test_get_profile_reads_ui_mode(self):
        accounts = FakeAccounts()

        class R:
            @staticmethod
            def json():
                return [
                    {
                        "id": "u1",
                        "display_name": None,
                        "is_invited": True,
                        "is_grandfathered": False,
                        "onboarding_completed_at": None,
                        "ui_mode": "TERMINAL",
                    }
                ]

        accounts._request = lambda method, path, **kwargs: R()  # type: ignore[method-assign]
        profile = Accounts.get_profile(accounts, "u1")

        assert profile.ui_mode == "TERMINAL"

    def test_get_profile_ui_mode_defaults_to_none(self):
        accounts = FakeAccounts()

        class R:
            @staticmethod
            def json():
                return [
                    {
                        "id": "u1",
                        "display_name": None,
                        "is_invited": True,
                        "is_grandfathered": False,
                        "onboarding_completed_at": None,
                    }
                ]

        accounts._request = lambda method, path, **kwargs: R()  # type: ignore[method-assign]
        profile = Accounts.get_profile(accounts, "u1")

        assert profile.ui_mode is None

    def test_set_ui_mode_patches_the_profile(self):
        accounts = FakeAccounts()
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))

            class R:
                @staticmethod
                def json():
                    return {}

            return R()

        accounts._request = fake_request  # type: ignore[method-assign]
        Accounts.set_ui_mode(accounts, "u1", "CONVENTIONAL")

        method, path, kwargs = calls[0]
        assert (method, path) == ("PATCH", "/profiles")
        assert kwargs["params"] == {"id": "eq.u1"}
        assert kwargs["json"]["ui_mode"] == "CONVENTIONAL"


class TestConfiguration:
    def test_unconfigured_accounts_report_it(self):
        accounts = Accounts(base_url="", service_key="")
        assert accounts.configured is False

    def test_unconfigured_calls_raise_rather_than_silently_no_op(self):
        accounts = Accounts(base_url="", service_key="")
        with pytest.raises(AccountsError, match="not configured"):
            accounts.get_profile("u1")


class TestMonthlyUsage:
    def test_filters_to_the_current_calendar_month(self):
        """monthly_usage's name and docstring both promise 'this month', but

        nothing filtered by date at all until B.10 wired quota enforcement
        to it and this gap would have meant every user's lifetime completed
        count, never resetting.
        """
        accounts = FakeAccounts()
        captured = {}

        class R:
            headers = {"content-range": "0-0/3"}

            @staticmethod
            def json():
                return []

        def fake_request(method, path, **kwargs):
            captured["method"], captured["path"], captured["kwargs"] = (
                method,
                path,
                kwargs,
            )
            return R()

        accounts._request = fake_request  # type: ignore[method-assign]
        result = Accounts.monthly_usage(accounts, "u1")

        assert result == 3
        month_start = (
            datetime.now(UTC)
            .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )
        assert captured["kwargs"]["params"]["created_at"] == f"gte.{month_start}"
        assert captured["kwargs"]["params"]["profile_id"] == "eq.u1"
        assert captured["kwargs"]["params"]["outcome"] == "eq.completed"


class TestReferralBonus:
    def test_six_referrals_grant_three_bonus_analyses(self):
        # Product decision 2026-09-15, pinned so the numbers can't drift
        # silently away from what's reported to the user.
        assert (REFERRAL_CONVERSIONS_PER_BONUS, REFERRAL_BONUS_ANALYSES) == (6, 3)

    def test_no_bonus_below_the_threshold(self):
        assert bonus_analyses_for(0) == 0
        assert bonus_analyses_for(5) == 0

    def test_one_bonus_at_the_threshold(self):
        assert bonus_analyses_for(6) == 3

    def test_bonus_accumulates_per_complete_group(self):
        assert bonus_analyses_for(11) == 3
        assert bonus_analyses_for(18) == 9

    def test_next_reward_at_the_first_threshold(self):
        assert next_reward_at(0) == 6
        assert next_reward_at(5) == 6

    def test_next_reward_at_the_following_threshold(self):
        assert next_reward_at(6) == 12
        assert next_reward_at(9) == 12

    def test_effective_allowance_adds_the_bonus_to_the_free_tier(self):
        assert effective_allowance(0) == FREE_MONTHLY_ANALYSES
        assert effective_allowance(6) == FREE_MONTHLY_ANALYSES + 3


class TestReferralCode:
    def test_returns_the_existing_code_without_writing(self):
        accounts = FakeAccounts()
        accounts.profile_row = {
            "id": "u1",
            "display_name": None,
            "is_invited": True,
            "is_grandfathered": False,
            "onboarding_completed_at": None,
            "referral_code": "ABCD1234",
        }

        def fake_request(method, path, **kwargs):
            if method == "GET" and path == "/profiles":

                class R:
                    @staticmethod
                    def json():
                        return [accounts.profile_row]

                return R()
            raise AssertionError(f"unexpected write: {method} {path}")

        accounts._request = fake_request  # type: ignore[method-assign]
        assert Accounts.get_referral_code(accounts, "u1") == "ABCD1234"

    def test_assigns_and_persists_a_new_code_when_absent(self):
        """A single conditional PATCH, not a check-then-set: see

        _assign_referral_code's docstring for the TOCTOU it replaces.
        """
        accounts = FakeAccounts()
        calls = []

        class R:
            def __init__(self, rows):
                self.status_code = 200
                self._rows = rows

            def json(self):
                return self._rows

        def fake_request_allow(method, path, ok_extra=(), **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET" and path == "/profiles":
                return R(
                    [
                        {
                            "id": "u1",
                            "display_name": None,
                            "is_invited": True,
                            "is_grandfathered": False,
                            "onboarding_completed_at": None,
                            "referral_code": None,
                        }
                    ]
                )
            assert method == "PATCH" and path == "/profiles"
            code = kwargs["json"]["referral_code"]
            return R([{"referral_code": code}])

        accounts._request = fake_request_allow  # type: ignore[method-assign]
        accounts._request_allow = fake_request_allow  # type: ignore[method-assign]
        code = Accounts.get_referral_code(accounts, "u1")

        assert isinstance(code, str) and len(code) == 8
        patch_calls = [c for c in calls if c[0] == "PATCH"]
        assert len(patch_calls) == 1
        assert patch_calls[0][2]["json"]["referral_code"] == code
        assert patch_calls[0][2]["params"] == {
            "id": "eq.u1",
            "referral_code": "is.null",
        }

    def test_retries_on_a_collision(self):
        """A 409 (the guessed code collided with someone else's row) tries a

        fresh random code, rather than retrying the same one or 503ing.
        """
        accounts = FakeAccounts()
        attempts = {"count": 0}

        class R:
            def __init__(self, status_code, rows=None):
                self.status_code = status_code
                self._rows = rows or []

            def json(self):
                return self._rows

        def fake_request_allow(method, path, ok_extra=(), **kwargs):
            if method == "GET" and path == "/profiles":
                return R(
                    200,
                    [
                        {
                            "id": "u1",
                            "display_name": None,
                            "is_invited": True,
                            "is_grandfathered": False,
                            "onboarding_completed_at": None,
                            "referral_code": None,
                        }
                    ],
                )
            assert method == "PATCH" and path == "/profiles"
            attempts["count"] += 1
            if attempts["count"] == 1:
                return R(409)
            code = kwargs["json"]["referral_code"]
            return R(200, [{"referral_code": code}])

        accounts._request = fake_request_allow  # type: ignore[method-assign]
        accounts._request_allow = fake_request_allow  # type: ignore[method-assign]
        Accounts.get_referral_code(accounts, "u1")

        assert attempts["count"] == 2

    def test_a_concurrent_winner_is_read_back_on_zero_rows(self):
        """0 rows back means the conditional match failed: someone else's

        concurrent request already set referral_code first. Re-read it
        rather than erroring or generating a second, orphaned code.
        """
        accounts = FakeAccounts()

        class R:
            def __init__(self, rows):
                self.status_code = 200
                self._rows = rows

            def json(self):
                return self._rows

        calls = {"get": 0}

        def fake_request_allow(method, path, ok_extra=(), **kwargs):
            if method == "GET" and path == "/profiles":
                calls["get"] += 1
                referral_code = None if calls["get"] == 1 else "WON1234"
                return R(
                    [
                        {
                            "id": "u1",
                            "display_name": None,
                            "is_invited": True,
                            "is_grandfathered": False,
                            "onboarding_completed_at": None,
                            "referral_code": referral_code,
                        }
                    ]
                )
            assert method == "PATCH" and path == "/profiles"
            return R([])  # conditional match failed — someone else won it

        accounts._request = fake_request_allow  # type: ignore[method-assign]
        accounts._request_allow = fake_request_allow  # type: ignore[method-assign]
        code = Accounts.get_referral_code(accounts, "u1")

        assert code == "WON1234"

    def test_raises_when_no_profile_exists(self):
        accounts = FakeAccounts()

        def fake_request(method, path, **kwargs):
            class R:
                @staticmethod
                def json():
                    return []

            return R()

        accounts._request = fake_request  # type: ignore[method-assign]
        with pytest.raises(AccountsError, match="no profile"):
            Accounts.get_referral_code(accounts, "u1")


class TestReferralCounts:
    def test_reads_referred_and_converted_counts(self):
        accounts = FakeAccounts()
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))

            class R:
                headers = {"content-range": "0-0/2"}

                @staticmethod
                def json():
                    return []

            return R()

        accounts._request = fake_request  # type: ignore[method-assign]
        counts = Accounts.referral_counts(accounts, "u1")

        assert (counts.referred_count, counts.converted_count) == (2, 2)
        assert calls[0][1] == "/referrals"
        assert calls[0][2]["params"]["referrer_profile_id"] == "eq.u1"
        assert "converted_at" not in calls[0][2]["params"]
        assert calls[1][2]["params"]["converted_at"] == "not.is.null"


def _profile(**overrides) -> Profile:
    defaults = dict(
        id="referred-1",
        display_name=None,
        is_invited=True,
        is_grandfathered=False,
        onboarding_completed_at=None,
    )
    defaults.update(overrides)
    return Profile(**defaults)


class TestSyncReferralAttribution:
    def test_skips_when_already_attributed(self):
        accounts = FakeAccounts()
        profile = _profile(referred_by="someone")

        def boom(*args, **kwargs):
            raise AssertionError("must not make any request once already attributed")

        accounts._request = boom  # type: ignore[method-assign]
        accounts._request_allow = boom  # type: ignore[method-assign]
        result = Accounts.sync_referral_attribution(
            accounts, profile, "referred@example.com"
        )
        assert result is True

    def test_skips_when_no_email(self):
        accounts = FakeAccounts()
        profile = _profile()

        def boom(*args, **kwargs):
            raise AssertionError("must not make any request with no email")

        accounts._request = boom  # type: ignore[method-assign]
        accounts._request_allow = boom  # type: ignore[method-assign]
        result = Accounts.sync_referral_attribution(accounts, profile, None)
        assert result is False

    def test_attributes_via_the_waitlist_referral_code(self):
        accounts = FakeAccounts()
        profile = _profile()
        calls = []

        class R:
            def __init__(self, rows):
                self.status_code = 200
                self._rows = rows

            def json(self):
                return self._rows

        def fake(method, path, ok_extra=(), **kwargs):
            calls.append((method, path, kwargs))
            if path == "/waitlist":
                return R([{"referral_code": "REF12345"}])
            if path == "/profiles" and method == "GET":
                return R([{"id": "referrer-1"}])
            return R([])

        accounts._request = fake  # type: ignore[method-assign]
        accounts._request_allow = fake  # type: ignore[method-assign]
        result = Accounts.sync_referral_attribution(
            accounts, profile, "referred@example.com"
        )
        assert result is True

        posts = [c for c in calls if c[0] == "POST" and c[1] == "/referrals"]
        assert posts == [
            (
                "POST",
                "/referrals",
                posts[0][2],
            )
        ]
        assert posts[0][2]["json"] == {
            "referrer_profile_id": "referrer-1",
            "referred_profile_id": "referred-1",
        }
        patches = [c for c in calls if c[0] == "PATCH" and c[1] == "/profiles"]
        assert patches[0][2]["json"]["referred_by"] == "referrer-1"

    def test_matches_the_waitlist_email_case_insensitively(self):
        """The JWT's email is lowercase; the waitlist row keeps whatever case

        the visitor originally typed (the unique index is on lower(email),
        not the column itself), so an exact `eq` match would silently never
        attribute a mixed-case signup.
        """
        accounts = FakeAccounts()
        profile = _profile()
        calls = []

        class R:
            def __init__(self, rows):
                self.status_code = 200
                self._rows = rows

            def json(self):
                return self._rows

        def fake(method, path, ok_extra=(), **kwargs):
            calls.append((method, path, kwargs))
            if path == "/waitlist":
                assert kwargs["params"]["email"] == "ilike.Jane@Example.com"
                return R([{"referral_code": "REF12345"}])
            if path == "/profiles" and method == "GET":
                return R([{"id": "referrer-1"}])
            return R([])

        accounts._request = fake  # type: ignore[method-assign]
        accounts._request_allow = fake  # type: ignore[method-assign]
        result = Accounts.sync_referral_attribution(
            accounts, profile, "Jane@Example.com"
        )
        assert result is True
        waitlist_calls = [c for c in calls if c[1] == "/waitlist"]
        assert len(waitlist_calls) == 1

    def test_escapes_ilike_wildcards_in_the_email(self):
        """A literal '*' in an (unusual but valid) email must not act as a

        PostgREST ilike wildcard and match unrelated waitlist rows.
        """
        accounts = FakeAccounts()
        profile = _profile()

        class R:
            def __init__(self, rows):
                self.status_code = 200
                self._rows = rows

            def json(self):
                return self._rows

        captured = {}

        def fake(method, path, ok_extra=(), **kwargs):
            if path == "/waitlist":
                captured["email_param"] = kwargs["params"]["email"]
                return R([])
            raise AssertionError(f"unexpected write: {method} {path}")

        accounts._request = fake  # type: ignore[method-assign]
        accounts._request_allow = fake  # type: ignore[method-assign]
        Accounts.sync_referral_attribution(accounts, profile, "we*rd@example.com")

        assert captured["email_param"] == "ilike.we\\*rd@example.com"

    def test_ignores_self_referral(self):
        accounts = FakeAccounts()
        profile = _profile(id="same-user")

        class R:
            def __init__(self, rows):
                self.status_code = 200
                self._rows = rows

            def json(self):
                return self._rows

        def fake(method, path, ok_extra=(), **kwargs):
            if path == "/waitlist":
                return R([{"referral_code": "OWN12345"}])
            if path == "/profiles" and method == "GET":
                return R([{"id": "same-user"}])
            raise AssertionError(f"unexpected write: {method} {path}")

        accounts._request = fake  # type: ignore[method-assign]
        accounts._request_allow = fake  # type: ignore[method-assign]
        result = Accounts.sync_referral_attribution(
            accounts, profile, "referred@example.com"
        )
        assert result is False

    def test_no_waitlist_row_is_a_no_op(self):
        accounts = FakeAccounts()
        profile = _profile()

        class R:
            def __init__(self, rows):
                self.status_code = 200
                self._rows = rows

            def json(self):
                return self._rows

        def fake(method, path, ok_extra=(), **kwargs):
            if path == "/waitlist":
                return R([])
            raise AssertionError(f"unexpected write: {method} {path}")

        accounts._request = fake  # type: ignore[method-assign]
        accounts._request_allow = fake  # type: ignore[method-assign]
        result = Accounts.sync_referral_attribution(
            accounts, profile, "referred@example.com"
        )
        assert result is False

    def test_a_409_on_the_referral_insert_is_treated_as_already_attributed(self):
        """The referrals_referred_once unique index makes this idempotent —

        a concurrent /me call that already attributed the same profile must
        not raise here, and still reports the profile as referred.
        """
        accounts = FakeAccounts()
        profile = _profile()

        class R:
            def __init__(self, status_code, rows=None):
                self.status_code = status_code
                self._rows = rows or []

            def json(self):
                return self._rows

        def fake(method, path, ok_extra=(), **kwargs):
            if path == "/waitlist":
                return R(200, [{"referral_code": "REF12345"}])
            if path == "/profiles" and method == "GET":
                return R(200, [{"id": "referrer-1"}])
            if path == "/referrals":
                assert 409 in ok_extra
                return R(409)
            return R(200)

        accounts._request = fake  # type: ignore[method-assign]
        accounts._request_allow = fake  # type: ignore[method-assign]
        result = Accounts.sync_referral_attribution(
            accounts, profile, "referred@example.com"
        )
        assert result is True

    def test_a_store_failure_never_propagates(self):
        """Best-effort, same posture as record_usage: /me must keep working."""
        accounts = FakeAccounts()
        profile = _profile()

        def boom(*args, **kwargs):
            raise AccountsError("store down")

        accounts._request = boom  # type: ignore[method-assign]
        accounts._request_allow = boom  # type: ignore[method-assign]
        Accounts.sync_referral_attribution(accounts, profile, "referred@example.com")


class TestMarkReferralConverted:
    def test_patches_the_referral_row_conditionally(self):
        accounts = FakeAccounts()
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))

            class R:
                @staticmethod
                def json():
                    return {}

            return R()

        accounts._request = fake_request  # type: ignore[method-assign]
        Accounts.mark_referral_converted(accounts, "referred-1")

        method, path, kwargs = calls[0]
        assert (method, path) == ("PATCH", "/referrals")
        assert kwargs["params"] == {
            "referred_profile_id": "eq.referred-1",
            "converted_at": "is.null",
        }
        assert kwargs["json"] == {"converted_at": "now()"}

    def test_a_store_failure_never_propagates(self):
        accounts = FakeAccounts()

        def boom(*args, **kwargs):
            raise AccountsError("store down")

        accounts._request = boom  # type: ignore[method-assign]
        Accounts.mark_referral_converted(accounts, "referred-1")
