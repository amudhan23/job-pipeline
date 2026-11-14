"""When to read a board again.

We want to see a new posting as soon as possible after it goes live, while
spending requests where postings actually appear. Each board keeps an
estimate λ of how many relevant new postings it gets per day (an
exponentially weighted average of what each visit found). The revisit
interval shrinks with √λ — the sublinear relation Cho & Garcia-Molina show
minimises the expected age of what you've missed — clamped to [min, max],
and tightened during U.S. working hours, when companies publish jobs.

  λ ≈ 10/day  → ~3 min      (a big company hiring hard)
  λ ≈ 1/day   → ~10 min
  λ ≈ 0.1/day → ~30 min
  λ = 0       → ~70 min     (still checked every hour or so)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


@dataclass
class Policy:
    base_minutes: float = 10.0      # interval at λ = 1/day, before the time-of-day factor
    rate_floor: float = 0.02        # keeps quiet boards from going to "never"
    min_seconds: float = 120.0
    max_seconds: float = 3 * 3600.0
    tau_days: float = 7.0           # memory of the rate estimate
    slow_platforms: tuple[str, ...] = ("workday", "smartrecruiters", "bamboohr", "workable")
    slow_min_seconds: float = 30 * 60.0   # these need a request per job; go easy

    def time_factor(self, now: datetime | None = None) -> float:
        """Most postings go live on U.S. weekdays in office hours."""
        t = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
        if t.weekday() >= 5:
            return 1.6
        if 8 <= t.hour < 19:
            return 0.6
        if 6 <= t.hour < 8 or 19 <= t.hour < 23:
            return 1.0
        return 1.6

    def interval(self, rate_per_day: float, platform: str = "", now: datetime | None = None) -> float:
        seconds = self.base_minutes * 60.0 / math.sqrt(max(rate_per_day, 0.0) + self.rate_floor)
        seconds *= self.time_factor(now)
        low = self.slow_min_seconds if platform in self.slow_platforms else self.min_seconds
        return max(low, min(self.max_seconds, seconds))

    def update_rate(self, rate_per_day: float, new_count: int, elapsed_seconds: float) -> float:
        """EWMA of new relevant postings per day. A visit after a long gap
        counts for more than one a minute after the last."""
        elapsed_days = max(elapsed_seconds, 1.0) / 86400.0
        weight = 1.0 - math.exp(-elapsed_days / self.tau_days)
        observed = new_count / elapsed_days
        # One posting seen after a 2-minute gap would read as 720/day: cap
        # what a single visit can claim
        observed = min(observed, 50.0)
        return (1.0 - weight) * rate_per_day + weight * observed


# Starting estimates: a board a feed just pointed us to is actively hiring
INITIAL_RATE = {"feed": 2.0, "web": 0.5, "pinned": 1.0}
