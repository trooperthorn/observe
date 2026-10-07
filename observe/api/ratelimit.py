"""Token buckets for the v2 surface (docs/DATA-API-DESIGN.md section 4.8).

A bucket holds up to `burst` tokens and refills at `rate` tokens a second. A request takes its
cost (a metrics query costs more than a list). The limits are in memory and reset on restart.
The number of keys is bounded: when the table is full, buckets that are full again are dropped,
and a flood of new keys shares one overflow bucket.
"""

from __future__ import annotations

import math
from collections.abc import Callable


class TokenBucket:
    MAX_KEYS = 4096
    OVERFLOW = "overflow"

    def __init__(self, rate: float, burst: float, clock: Callable[[], float]) -> None:
        self.rate = rate
        self.burst = burst
        self._clock = clock
        self._state: dict[str, list[float]] = {}  # key -> [tokens, last refill time]

    def _bucket(self, key: str, now: float) -> list[float]:
        if key not in self._state and len(self._state) >= self.MAX_KEYS:
            self._state = {k: v for k, v in self._state.items()
                           if v[0] + (now - v[1]) * self.rate < self.burst}
            if len(self._state) >= self.MAX_KEYS:
                key = self.OVERFLOW
        entry = self._state.get(key)
        if entry is None:
            entry = self._state[key] = [self.burst, now]
        else:
            entry[0] = min(self.burst, entry[0] + (now - entry[1]) * self.rate)
            entry[1] = now
        return entry

    def take(self, key: str, cost: float = 1.0) -> int:
        """Take `cost` tokens. Returns 0 when allowed, else the whole seconds to wait."""
        cost = min(cost, self.burst)
        entry = self._bucket(key, self._clock())
        if entry[0] >= cost:
            entry[0] -= cost
            return 0
        return max(1, math.ceil((cost - entry[0]) / self.rate))

    def exhausted(self, key: str) -> bool:
        """True when the key has less than one token. Takes nothing."""
        return self._bucket(key, self._clock())[0] < 1.0
