from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        now = self._clock()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True
