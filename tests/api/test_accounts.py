import pytest

from src.api.accounts import (
    FREE_MONTHLY_ANALYSES,
    MAX_INTERESTS,
    MIN_INTERESTS,
    Accounts,
    AccountsError,
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


class TestConfiguration:
    def test_unconfigured_accounts_report_it(self):
        accounts = Accounts(base_url="", service_key="")
        assert accounts.configured is False

    def test_unconfigured_calls_raise_rather_than_silently_no_op(self):
        accounts = Accounts(base_url="", service_key="")
        with pytest.raises(AccountsError, match="not configured"):
            accounts.get_profile("u1")
