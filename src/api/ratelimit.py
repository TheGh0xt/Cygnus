"""Rate limiting for expensive endpoints.

Starting an analysis costs real money — four model stages plus grounded
search — so the limit exists to protect the project's Gemini budget, not to
ration a scarce resource. That shapes the design: a per-user cap stops one
account running away with it, and a global cap stops many individually
well-behaved accounts exhausting it collectively.

Sliding window rather than fixed. A fixed window lets a caller spend the
whole budget in the last second of one window and again in the first second
of the next, which is a 2x burst exactly when you least want one.

In-process state, deliberately. It matches the analysis registry, which is
also per-process, and Cygnus runs a single worker for that reason. Running
more than one worker needs shared storage for both — see the note in the
Render blueprint.
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict, deque
from collections.abc import Callable

logger = logging.getLogger("cygnus.api.ratelimit")

_GLOBAL_KEY = "__global__"


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int, scope: str):
        super().__init__(f"rate limit exceeded ({scope})")
        # Seconds until the caller could succeed. Never zero: a Retry-After of
        # 0 invites an immediate retry that is guaranteed to fail again.
        self.retry_after = retry_after
        self.scope = scope


class RateLimiter:
    def __init__(
        self,
        per_user: int = 10,
        per_user_window: float = 3600,
        global_limit: int = 0,
        global_window: float = 3600,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._per_user = per_user
        self._per_user_window = per_user_window
        self._global_limit = global_limit
        self._global_window = global_window
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, user_key: str) -> None:
        """Record a call, or raise if it would exceed a limit.

        Both limits are evaluated before either is recorded, so a request
        rejected by the global cap does not consume the user's budget too.
        """
        now = self._clock()

        self._prune(now)

        if self._per_user > 0:
            self._assert_under(
                user_key, self._per_user, self._per_user_window, now, "per-user"
            )
        if self._global_limit > 0:
            self._assert_under(
                _GLOBAL_KEY, self._global_limit, self._global_window, now, "global"
            )

        if self._per_user > 0:
            self._hits[user_key].append(now)
        if self._global_limit > 0:
            self._hits[_GLOBAL_KEY].append(now)

    def _assert_under(
        self, key: str, limit: int, window: float, now: float, scope: str
    ) -> None:
        hits = self._hits[key]
        while hits and now - hits[0] >= window:
            hits.popleft()
        if len(hits) < limit:
            return
        # The oldest call in the window is the one whose expiry frees a slot.
        retry_after = max(1, math.ceil(window - (now - hits[0])))
        logger.info("rate limit hit (%s) for %s", scope, key)
        raise RateLimitExceeded(retry_after=retry_after, scope=scope)

    def _prune(self, now: float) -> None:
        """Drop keys with no recent calls.

        Without this, every user who ever called leaks an entry for the
        lifetime of the process.
        """
        # Only windows belonging to an *enabled* limit count. Including a
        # disabled limit's window here made pruning use the longer of the two
        # even when that limit was off, so entries were retained for an hour
        # under a one-minute per-user window and the map grew unbounded.
        windows = [
            w
            for w, limit in (
                (self._per_user_window, self._per_user),
                (self._global_window, self._global_limit),
            )
            if limit > 0
        ]
        if not windows:
            return
        window = max(windows)
        stale = [
            key
            for key, hits in self._hits.items()
            if not hits or now - hits[-1] >= window
        ]
        for key in stale:
            del self._hits[key]

    def tracked_keys(self) -> int:
        return len(self._hits)
