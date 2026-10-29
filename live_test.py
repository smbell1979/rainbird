r"""Live check of the write commands the cycle-and-soak runner will rely on.

Answers, on this controller:
  1. Does a manual zone run start, and how long does the module take to ack it?
  2. Does stop work?
  3. Does a manual run still start while a rain delay is set? (The runner's
     fallback design suppresses the controller's own programs with a daily
     1-day rain delay, so manual runs must be unaffected.)

Every command is read back. Whatever happens, it finishes by sending stop and
restoring the original rain delay, and checks both. Runs are 1 minute, so if
the link dies the controller shuts the valve off on its own.

    .venv313\Scripts\python live_test.py            # prints the plan only
    .venv313\Scripts\python live_test.py --apply    # waters zone 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from pyrainbird.async_client import AsyncRainbirdClient, AsyncRainbirdController

from probe import (
    DEFAULT_HOST,
    DEFAULT_PASSWORD_FILE,
    LOG_DIR,
    MAX_RECENT_FAILURES,
    counts_as_password_attempt,
    describe_error,
    got_no_answer,
    local_now,
    read_password,
    recent_failures,
    record_failure,
    setup_logging,
)

ZONE = 1
SECOND_ZONE = 2
MINUTES = 1
SPACING_S = 1.0
# Writes are reported to take 15-20 s to ack on LNK2, past the 10 s timeout
# pyrainbird applies when it does the pacing, so pacing is done here instead
# and the session timeout set generously.
REQUEST_TIMEOUT_S = 30
# After an unanswered request the module tends to refuse connections for a
# few seconds, so retries wait this long first.
RETRY_PAUSE_S = 4.0
CONFIRM_TIMEOUT_S = 30
POLL_S = 2.0

PLAN = f"""\
Plan (zone {ZONE}, {MINUTES}-minute runs):
  pre:  check nothing is watering (abort if something is); read the current rain delay
  A:    start zone {ZONE} for {MINUTES} min; time the ack; poll until zone {ZONE} shows running
  B:    stop; poll until nothing is running
  C:    set rain delay to 1 day; read it back; start zone {ZONE} for {MINUTES} min;
        poll for running (this is the question); stop; poll until nothing is running
  end:  always stop and restore the original rain delay, reading both back"""


class Session:
    """Paced calls with one retry for reads and idempotent writes."""

    def __init__(self, controller: AsyncRainbirdController) -> None:
        self.c = controller
        self.last = 0.0
        self.events: list[dict[str, Any]] = []

    def log(self, event: str, **fields: Any) -> None:
        stamp = local_now().strftime("%H:%M:%S")
        detail = ", ".join(f"{k}={v}" for k, v in fields.items())
        print(f"  {stamp}  {event}{': ' + detail if detail else ''}", flush=True)
        self.events.append({"time": stamp, "event": event, **fields})

    async def call(self, fetch: Callable[[], Awaitable[Any]], retry: bool) -> Any:
        for attempt in (1, 2):
            wait = SPACING_S - (time.monotonic() - self.last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await fetch()
            except Exception as err:
                if attempt == 2 or not retry or not got_no_answer(err):
                    raise
                self.log("no answer, retrying", error=describe_error(err)[:80])
                await asyncio.sleep(RETRY_PAUSE_S)
            finally:
                self.last = time.monotonic()

    async def running(self) -> set[int]:
        states = await self.call(self.c.get_zone_states, retry=True)
        return set(states.active_set)

    async def wait_until(self, want: Callable[[set[int]], bool], label: str) -> bool:
        start = time.monotonic()
        while time.monotonic() - start < CONFIRM_TIMEOUT_S:
            read_start = time.monotonic()
            try:
                zones = await self.running()
            except Exception as err:
                self.log("state read failed", error=describe_error(err))
            else:
                # Every poll is logged so a slow confirmation can be told apart
                # from a slow module (the first live run couldn't).
                self.log("poll", running=sorted(zones),
                         read_s=round(time.monotonic() - read_start, 1))
                if want(zones):
                    self.log(f"confirmed {label}", after_s=round(time.monotonic() - start, 1),
                             running=sorted(zones))
                    return True
            await asyncio.sleep(POLL_S)
        self.log(f"NOT confirmed: {label}", waited_s=CONFIRM_TIMEOUT_S)
        return False

    async def send_start(self, zone: int) -> dict[str, Any]:
        start = time.monotonic()
        try:
            # retry=False: a start that timed out may still have been executed.
            await self.call(lambda: self.c.irrigate_zone(zone, MINUTES), retry=False)
            ack = {"acked": True, "ack_s": round(time.monotonic() - start, 1)}
        except Exception as err:
            ack = {"acked": False, "after_s": round(time.monotonic() - start, 1),
                   "error": describe_error(err)}
        self.log(f"start zone {zone} sent", **ack)
        return ack

    async def start_zone(self, zone: int = ZONE) -> dict[str, Any]:
        """Start a zone without ever sending a second start blindly."""
        ack = await self.send_start(zone)
        ack["running"] = await self.wait_until(lambda z: zone in z, f"zone {zone} running")
        return ack

    async def watch(self, seconds: float) -> list[tuple[float, list[int]]]:
        """Record which zones are running, polling for the given time."""
        seen = []
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            try:
                zones = sorted(await self.running())
            except Exception as err:
                self.log("state read failed", error=describe_error(err))
            else:
                seen.append((round(time.monotonic() - start, 1), zones))
                self.log("watch", t=seen[-1][0], running=zones)
            await asyncio.sleep(POLL_S)
        return seen

    async def stop(self) -> bool:
        try:
            await self.call(self.c.stop_irrigation, retry=True)
            self.log("stop sent")
        except Exception as err:
            self.log("stop not acked", error=describe_error(err))
        return await self.wait_until(lambda z: not z, "nothing running")

    async def set_delay(self, days: int) -> bool:
        try:
            await self.call(lambda: self.c.set_rain_delay(days), retry=True)
            value = await self.call(self.c.get_rain_delay, retry=True)
        except Exception as err:
            self.log(f"set rain delay {days} failed", error=describe_error(err))
            return False
        self.log("rain delay set", wanted=days, reads_back=value)
        return value == days


async def run(args: argparse.Namespace) -> int:
    print((QUEUE_PLAN if args.queue_test else PLAN) + "\n")
    if not args.apply:
        print("Dry run: nothing sent. Add --apply to water.")
        return 0

    password = read_password(args.password_file)
    if len(recent_failures(local_now())) >= MAX_RECENT_FAILURES:
        print("Refusing: recent password failures recorded (see probe.py). Nothing sent.")
        return 2

    results: dict[str, Any] = {"zone": ZONE, "minutes": MINUTES}
    LOG_DIR.mkdir(exist_ok=True)
    debug_log = LOG_DIR / f"live-{local_now():%Y%m%d-%H%M%S}.log"
    setup_logging(debug_log)
    print(f"debug log: {debug_log}")
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
    connector = aiohttp.TCPConnector(force_close=False)  # keep-alive: measured far more reliable
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as http:
        client = AsyncRainbirdClient(http, f"https://{args.host}/stick", password, ssl_context=False)
        s = Session(AsyncRainbirdController(client))
        try:
            await s.call(s.c.get_model_and_version, retry=False)
        except Exception as err:
            if counts_as_password_attempt(err):
                record_failure(local_now())
            print(f"First contact failed, nothing sent: {describe_error(err)}")
            return 1

        zones = await s.running()
        original_delay = await s.call(s.c.get_rain_delay, retry=True)
        s.log("pre", running=sorted(zones), rain_delay=original_delay)
        if zones:
            # Checked before the try: the cleanup's stop would otherwise cut
            # off watering this script didn't start.
            print("Something is already watering; not interfering. Nothing sent.")
            return 1

        if args.queue_test:
            return await queue_test(s, results)

        try:
            print("\nA: start")
            results["A_start"] = await s.start_zone()
            print("\nB: stop")
            results["B_stop_confirmed"] = await s.stop()

            await asyncio.sleep(5)
            print("\nC: manual run during a rain delay")
            results["C_delay_set"] = await s.set_delay(1)
            if results["C_delay_set"]:
                results["C_start_during_delay"] = await s.start_zone()
                results["C_stop_confirmed"] = await s.stop()
        finally:
            print("\nend: cleanup")
            results["end_stopped"] = await s.stop()
            results["end_delay_restored"] = await s.set_delay(original_delay)
            if not results["end_stopped"]:
                print(f"** Could not confirm zone {ZONE} is off. It was started for {MINUTES} min, "
                      "so the controller should stop it by itself; check the panel. **")
            if not results["end_delay_restored"]:
                print(f"** Rain delay may not be back to {original_delay}. Check the panel or app. **")
            results["events"] = s.events
            LOG_DIR.mkdir(exist_ok=True)
            path = LOG_DIR / f"live-{local_now():%Y%m%d-%H%M%S}.json"
            path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
            print(f"\nresults: {path}")

    summary = {k: v for k, v in results.items() if k != "events"}
    print(json.dumps(summary, indent=2, default=str))
    return 0


QUEUE_PLAN = f"""\
Queue test (zones {ZONE} and {SECOND_ZONE}, {MINUTES}-minute runs):
  start zone {ZONE}; confirm it runs; start zone {SECOND_ZONE} while {ZONE} is running;
  watch which zones run for up to {MINUTES * 60 + 30} s:
    only {SECOND_ZONE} at once  -> the second command replaced the first
    {ZONE} then {SECOND_ZONE}         -> it queued
    both at once      -> it ran them together
  end:  always stop, reading it back"""


async def queue_test(s: Session, results: dict[str, Any]) -> int:
    try:
        results["first"] = await s.start_zone(ZONE)
        if not results["first"]["running"]:
            print(f"Zone {ZONE} never showed running; stopping without the second command.")
            return 1
        results["second"] = await s.send_start(SECOND_ZONE)
        seen = await s.watch(MINUTES * 60 + 30)
        results["seen"] = seen
        states = [tuple(z) for _, z in seen]
        if (ZONE, SECOND_ZONE) in states:
            verdict = "both ran at once"
        elif states and states[0] == (SECOND_ZONE,):
            verdict = "replaced: the second command took over immediately"
        elif (SECOND_ZONE,) in states and (ZONE,) in states:
            verdict = "queued: the second zone ran after the first finished"
        elif states and all(st == (ZONE,) or st == () for st in states):
            verdict = "ignored: the second command never ran"
        else:
            verdict = "unclear; see the watch log"
        results["verdict"] = verdict
        print(f"\nverdict: {verdict}")
    finally:
        print("\nend: cleanup")
        results["end_stopped"] = await s.stop()
        if not results["end_stopped"]:
            print(f"** Could not confirm everything is off. Runs were {MINUTES} min, so the "
                  "controller should stop them by itself; check the panel. **")
        results["events"] = s.events
        path = LOG_DIR / f"live-queue-{local_now():%Y%m%d-%H%M%S}.json"
        path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        print(f"results: {path}")
    return 0


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--password-file", type=type(DEFAULT_PASSWORD_FILE), default=DEFAULT_PASSWORD_FILE)
    parser.add_argument("--apply", action="store_true", help="actually send commands (waters zone 1)")
    parser.add_argument("--queue-test", action="store_true",
                        help=f"instead: start zone {ZONE}, then {SECOND_ZONE}, and see what happens")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
