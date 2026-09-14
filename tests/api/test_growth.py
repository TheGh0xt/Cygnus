import pytest

from src.api.growth import Growth, GrowthError, WaitlistJoinResult


class FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class FakeGrowth(Growth):
    """Growth with the HTTP layer replaced, so the rules are testable."""

    def __init__(self):
        super().__init__(base_url="https://example.test", service_key="service-key")
        self.calls: list[tuple[str, str, dict]] = []
        self.next_response = FakeResponse(201)

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.next_response


def test_join_waitlist_inserts_a_row():
    growth = FakeGrowth()
    result = growth.join_waitlist("someone@example.com", None)

    assert result == WaitlistJoinResult(already_registered=False)
    method, path, kwargs = growth.calls[0]
    assert (method, path) == ("POST", "/waitlist")
    assert kwargs["json"]["email"] == "someone@example.com"


def test_join_waitlist_carries_referral_code():
    growth = FakeGrowth()
    growth.join_waitlist("someone@example.com", "ref-123")

    _, _, kwargs = growth.calls[0]
    assert kwargs["json"]["referral_code"] == "ref-123"


def test_join_waitlist_is_idempotent_on_conflict():
    """A duplicate email is a 409 from PostgREST's unique index, not an error.

    Two people trying the landing page twice with the same address must not
    see a failure — they already joined, and that is success from their
    point of view.
    """
    growth = FakeGrowth()
    growth.next_response = FakeResponse(409, "duplicate key value")

    result = growth.join_waitlist("someone@example.com", None)

    assert result == WaitlistJoinResult(already_registered=True)


def test_join_waitlist_raises_on_other_failures():
    growth = FakeGrowth()
    growth.next_response = FakeResponse(500, "internal error")

    with pytest.raises(GrowthError):
        growth.join_waitlist("someone@example.com", None)


def test_not_configured_raises():
    growth = Growth(base_url="", service_key="")
    with pytest.raises(GrowthError, match="not configured"):
        growth.join_waitlist("someone@example.com", None)
