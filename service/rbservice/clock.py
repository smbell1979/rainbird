"""Time for the scheduler, swappable so whole runs can be tested in seconds."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from zoneinfo import ZoneInfo


class Clock:
    """Real time in a fixed zone (set explicitly, not taken from the host)."""

    def __init__(self, tz: str) -> None:
        self.tz = ZoneInfo(tz)

    def now(self) -> dt.datetime:
        return dt.datetime.now(self.tz)

    def wall(self) -> float:
        return time.time()

    def elapsed(self) -> float:
        """Seconds on a monotonic scale, for measuring a run."""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ScaledClock(Clock):
    """Plan time runs `speed` times faster than real time.

    Only the scheduler's own waits are scaled (segment lengths, soaks, polls).
    The link's request spacing stays real, because it protects the module.
    """

    def __init__(self, tz: str, speed: float, start: dt.datetime | None = None) -> None:
        super().__init__(tz)
        self.speed = speed
        self._real0 = time.monotonic()
        self._start = start or dt.datetime.now(self.tz)

    def elapsed(self) -> float:
        return (time.monotonic() - self._real0) * self.speed

    def now(self) -> dt.datetime:
        return self._start + dt.timedelta(seconds=self.elapsed())

    def wall(self) -> float:
        return self.now().timestamp()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self.speed)
