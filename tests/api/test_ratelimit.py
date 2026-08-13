import pytest

from src.api.ratelimit import RateLimiter, RateLimitExceeded


class Clock:
    """Controllable time, so window expiry is tested without sleeping."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestPerUserLimit:
    def test_allows_up_to_the_limit(self):
        clock = Clock()
        limiter = RateLimiter(per_user=3, per_user_window=60, clock=clock)
        for _ in range(3):
            limiter.check("user-1")

    def test_rejects_the_one_over(self):
        clock = Clock()
        limiter = RateLimiter(per_user=2, per_user_window=60, clock=clock)
        limiter.check("user-1")
        limiter.check("user-1")
        with pytest.raises(RateLimitExceeded):
            limiter.check("user-1")

    def test_users_have_separate_budgets(self):
        # One noisy user must not be able to lock everyone else out.
        clock = Clock()
        limiter = RateLimiter(per_user=1, per_user_window=60, clock=clock)
        limiter.check("user-1")
        limiter.check("user-2")

    def test_window_expiry_restores_budget(self):
        clock = Clock()
        limiter = RateLimiter(per_user=1, per_user_window=60, clock=clock)
        limiter.check("user-1")
        with pytest.raises(RateLimitExceeded):
            limiter.check("user-1")
        clock.advance(61)
        limiter.check("user-1")

    def test_window_slides_rather_than_resetting_wholesale(self):
        # A fixed window lets someone spend the whole budget at the end of one
        # window and again at the start of the next.
        clock = Clock()
        limiter = RateLimiter(per_user=2, per_user_window=60, clock=clock)
        limiter.check("u")
        clock.advance(59)
        limiter.check("u")
        clock.advance(2)  # the first call has aged out, the second has not
        limiter.check("u")
        with pytest.raises(RateLimitExceeded):
            limiter.check("u")


class TestGlobalLimit:
    def test_global_ceiling_applies_across_users(self):
        # Protects the Gemini budget: many invited users, each individually
        # under their own limit, can still exhaust it collectively.
        clock = Clock()
        limiter = RateLimiter(
            per_user=10,
            per_user_window=60,
            global_limit=2,
            global_window=60,
            clock=clock,
        )
        limiter.check("user-1")
        limiter.check("user-2")
        with pytest.raises(RateLimitExceeded):
            limiter.check("user-3")

    def test_global_window_also_expires(self):
        clock = Clock()
        limiter = RateLimiter(
            per_user=10,
            per_user_window=60,
            global_limit=1,
            global_window=60,
            clock=clock,
        )
        limiter.check("user-1")
        with pytest.raises(RateLimitExceeded):
            limiter.check("user-2")
        clock.advance(61)
        limiter.check("user-2")


class TestRetryAfter:
    def test_reports_when_to_retry(self):
        clock = Clock()
        limiter = RateLimiter(per_user=1, per_user_window=60, clock=clock)
        limiter.check("u")
        clock.advance(20)
        with pytest.raises(RateLimitExceeded) as caught:
            limiter.check("u")
        # 60s window, 20s elapsed — the oldest call ages out in 40s.
        assert 39 <= caught.value.retry_after <= 41

    def test_retry_after_is_never_zero(self):
        # A Retry-After of 0 invites an immediate retry that will also fail.
        clock = Clock()
        limiter = RateLimiter(per_user=1, per_user_window=1, clock=clock)
        limiter.check("u")
        clock.advance(0.99)
        with pytest.raises(RateLimitExceeded) as caught:
            limiter.check("u")
        assert caught.value.retry_after >= 1


class TestDisabled:
    def test_zero_means_unlimited(self):
        # Local development and tests should not have to reason about limits.
        limiter = RateLimiter(per_user=0, global_limit=0)
        for _ in range(50):
            limiter.check("u")


class TestBookkeeping:
    def test_old_entries_do_not_accumulate_forever(self):
        # Without pruning, every user who ever called leaks a growing list.
        clock = Clock()
        limiter = RateLimiter(per_user=5, per_user_window=60, clock=clock)
        for index in range(20):
            limiter.check(f"user-{index}")
        clock.advance(120)
        limiter.check("user-fresh")
        assert limiter.tracked_keys() <= 2
