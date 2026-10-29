"""The one connection to the LNK2 module.

Everything that talks to the controller goes through Link, which:
- keeps a single kept-alive HTTPS connection (measured on this module: ~0.25 s
  per reply kept alive, against ~4 s and 20% unanswered with a fresh TLS
  handshake per request);
- serialises requests and spaces them, because the module serves one request
  at a time;
- retries reads and idempotent writes once, after a pause, because after an
  unanswered request the module tends to refuse connections for a few seconds;
- never re-sends a zone start blindly: a start that timed out may have been
  executed, so the zone state is read back first;
- stops all access after a password rejection. The module locks out for 15
  minutes after 3 bad passwords, and a service retrying in a loop would keep it
  locked forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import aiohttp
from pyrainbird.async_client import AsyncRainbirdClient, AsyncRainbirdController
from pyrainbird.data import Schedule
from pyrainbird.exceptions import RainbirdAuthException, RainbirdConnectionError

from .clock import Clock

log = logging.getLogger(__name__)
T = TypeVar("T")

# Write acks are reported up to 15-20 s on LNK2, so the per-request limit is
# generous. pyrainbird's own pacing would cap it at 10 s, so pacing is done here.
REQUEST_TIMEOUT_S = 30.0


@dataclass
class StartResult:
    running: bool
    seen: set[int] | None
    """Zones the last successful read showed; None if no read succeeded."""


class LinkLockedOut(Exception):
    """The module rejected the password; no further requests are sent."""


@dataclass
class LinkHealth:
    connected: bool = False
    model: str | None = None
    firmware: str | None = None
    last_ok: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    consecutive_failures: int = 0
    auth_failed: bool = False


def read_password(path: Path) -> str:
    # utf-8-sig: Notepad may add a byte-order mark. Only the line ending is
    # removed; trimming more would quietly change the password.
    password = path.read_text(encoding="utf-8-sig").rstrip("\r\n")
    if not password or "\n" in password or password != password.strip():
        raise ValueError(f"{path} must hold only the controller password on one line")
    return password


def got_no_answer(err: BaseException) -> bool:
    """Transport failure (no reply) rather than the module replying with an error."""
    for candidate in (err, err.__cause__):
        if isinstance(candidate, aiohttp.ClientResponseError):
            return False
        if isinstance(candidate, (RainbirdConnectionError, aiohttp.ClientError, TimeoutError)):
            return True
    return False


def describe(err: BaseException) -> str:
    parts = [f"{type(err).__name__}: {err}"]
    if err.__cause__ is not None:
        parts.append(f"{type(err.__cause__).__name__}: {err.__cause__}")
    return " <- ".join(parts)


class Link:
    def __init__(
        self,
        host: str,
        password_file: Path,
        clock: Clock,
        spacing_s: float = 1.0,
        retry_pause_s: float = 4.0,
        confirm_s: float = 30.0,
        speed: float = 1.0,
        poll_s: float = 1.0,
    ) -> None:
        self.url = f"https://{host}/stick"
        self.password_file = password_file
        self.clock = clock
        self.spacing_s = spacing_s
        self.retry_pause_s = retry_pause_s
        # How long a start or stop may take to show in the zone state. Measured
        # ~1 s on this module, once 21 s, so 30 s before treating it as lost.
        self.confirm_s = confirm_s
        # Plan seconds per real second, so a run's length can be compared with
        # real read-back delays. 1 except against the sped-up simulator.
        self.speed = speed
        self.poll_s = poll_s  # between read-backs while confirming a start or stop
        self.health = LinkHealth()
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._owner: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._controller: AsyncRainbirdController | None = None

    async def open(self) -> None:
        password = read_password(self.password_file)
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(force_close=False),
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
        )
        client = AsyncRainbirdClient(self._session, self.url, password, ssl_context=False)
        self._controller = AsyncRainbirdController(client)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    @contextlib.asynccontextmanager
    async def _exclusive(self):
        """Hold the module for a sequence of requests (re-entrant per task).

        A start (or stop) and its confirmation reads must not have other
        requests queued between them. Otherwise a schedule read can delay the
        read-back past the zone's own run time, and "nothing running" then
        can't tell a lost start from a finished one (seen in testing).
        """
        me = asyncio.current_task()
        if self._owner is me:
            yield
            return
        async with self._lock:
            self._owner = me
            try:
                yield
            finally:
                self._owner = None

    async def _call(self, fetch: Callable[[AsyncRainbirdController], Awaitable[T]],
                    what: str, retry: bool) -> T:
        if self.health.auth_failed:
            raise LinkLockedOut("password was rejected; restart the service after fixing it")
        assert self._controller is not None, "Link.open() not called"
        async with self._exclusive():
            for attempt in (1, 2):
                wait = self.spacing_s - (time.monotonic() - self._last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    result = await fetch(self._controller)
                except RainbirdAuthException as err:
                    self._failed(what, err)
                    self.health.auth_failed = True
                    self.health.connected = False
                    raise LinkLockedOut(describe(err)) from err
                except Exception as err:
                    self._failed(what, err)
                    if attempt == 2 or not retry or not got_no_answer(err):
                        raise
                    log.info("%s: no answer, retrying in %.0f s", what, self.retry_pause_s)
                    await asyncio.sleep(self.retry_pause_s)
                else:
                    self.health.connected = True
                    self.health.last_ok = self.clock.wall()
                    self.health.consecutive_failures = 0
                    return result
                finally:
                    self._last_request = time.monotonic()
        raise AssertionError("unreachable")

    def _failed(self, what: str, err: BaseException) -> None:
        self.health.consecutive_failures += 1
        self.health.last_error = f"{what}: {describe(err)}"
        self.health.last_error_at = self.clock.wall()
        log.warning("%s failed: %s", what, describe(err))

    # --- reads ---------------------------------------------------------------

    async def identify(self) -> None:
        model = await self._call(lambda c: c.get_model_and_version(), "model", retry=True)
        fw = await self._call(lambda c: c.get_controller_firmware_version(), "firmware", retry=True)
        self.health.model = f"{model.model_name} ({model.model:04X})"
        self.health.firmware = f"{fw.major}.{fw.minor}.{fw.patch}"

    async def running(self) -> set[int]:
        states = await self._call(lambda c: c.get_zone_states(), "zone states", retry=True)
        return set(states.active_set)

    async def available_zones(self) -> set[int]:
        stations = await self._call(lambda c: c.get_available_stations(), "zones", retry=True)
        return set(stations.active_set)

    async def rain_delay(self) -> int:
        return int(await self._call(lambda c: c.get_rain_delay(), "rain delay", retry=True))

    async def rain_sensor_tripped(self) -> bool:
        return bool(await self._call(lambda c: c.get_rain_sensor_state(), "rain sensor", retry=True))

    async def schedule(self) -> Schedule:
        return await self._call(lambda c: c.get_schedule(), "schedule", retry=True)

    # --- writes, each confirmed by reading back --------------------------------

    async def set_rain_delay(self, days: int) -> bool:
        await self._call(lambda c: c.set_rain_delay(days), f"set rain delay {days}", retry=True)
        return await self.rain_delay() == days

    async def stop(self) -> bool:
        """Stop everything and confirm nothing is running. Safe to repeat."""
        async with self._exclusive():
            try:
                await self._call(lambda c: c.stop_irrigation(), "stop", retry=True)
            except LinkLockedOut:
                raise
            except Exception:
                pass  # the read-back decides
            return await self.wait_for(lambda zones: not zones, self.confirm_s) == set()

    async def start_zone(self, zone: int, minutes: int) -> StartResult:
        async with self._exclusive():
            return await self._start_zone(zone, minutes)

    async def _start_zone(self, zone: int, minutes: int) -> StartResult:
        """Start one zone and confirm it is running.

        The command carries its own run time, so the controller turns the valve
        off by itself even if this service dies mid-cycle.
        """
        if not 1 <= minutes <= 360:
            raise ValueError(f"run time {minutes} min is outside 1-360")
        what = f"start zone {zone} for {minutes} min"
        run_s = minutes * 60 / self.speed
        for attempt in (1, 2):
            sent = time.monotonic()
            try:
                await self._call(lambda c: c.irrigate_zone(zone, minutes), what, retry=False)
            except LinkLockedOut:
                raise
            except Exception as err:
                if not got_no_answer(err):
                    raise
            others: set[int] = set()
            first_read: list[float] = []

            def ours(zones: set[int]) -> bool:
                if not first_read:
                    first_read.append(time.monotonic())
                others.update(zones - {zone})
                return zone in zones

            seen = await self.wait_for(ours, self.confirm_s)
            if seen is not None and zone in seen:
                return StartResult(True, seen)
            if others:
                # Another zone took over at some point (the app, the panel, a
                # controller program), even if it has finished since. Re-sending
                # could cut off whoever started it, so give up instead.
                return StartResult(False, others)
            if seen is None:
                return StartResult(False, None)  # no successful read: can't tell, don't re-send
            if first_read[0] - sent > run_s / 2:
                # The first read that worked came so late the zone could have
                # run and finished already: "nothing running" can't tell a lost
                # start from a completed one, so re-sending risks watering twice.
                return StartResult(False, set())
            # A read early in the run showed nothing: the start was lost. Re-send once.
        return StartResult(False, set())

    async def wait_for(self, want: Callable[[set[int]], bool], timeout_s: float) -> set[int] | None:
        """Poll until want(zones); return the last zones successfully read (None if no read worked)."""
        deadline = time.monotonic() + timeout_s
        last: set[int] | None = None
        while True:
            try:
                last = await self.running()
                if want(last):
                    return last
            except LinkLockedOut:
                raise
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return last
            await asyncio.sleep(self.poll_s)
